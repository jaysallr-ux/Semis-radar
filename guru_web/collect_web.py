"""guru_web: third collector for Macro Guru Stance Watch (after guru_x and guru_yt).

What it does
1. Discovery: Google News RSS per tracked person (last 3 days), publisher URL decoded from the Google link.
2. Video bridge: when a news item is a TV/video page (CNBC, Yahoo video...), search YouTube for the same segment
   and hand the candidate to guru_yt (guru_web/data/yt_candidates.json). guru_yt verifies channel + upload date
   (must be within 1.5 days of the article date) and runs its normal Gemini + caption grading.
3. Article direct quotes: fetch the article body, let a light Gemini model propose the tracked person's direct
   quotes, then CODE verifies each quote is really in the article text and attributed near the person's name.
   Only verified quotes become 1차-인용. Gemini never grades its own output.
4. Official sources by the person: Carson Group blog posts whose author is Ryan Detrick (1차), Morgan Stanley
   "Thoughts on the Market" episode transcripts featuring Mike Wilson (observer, 1차).

What it never does
- Store article bodies or transcripts (only short verified quotes, titles, links).
- Change any stance. GPT reads web_feed.json and applies its own rules.

Clocks
- published_at: when the article/post was published (freshness source date)
- evidence_ready_at: when verified quotes first existed (GPT window clock, set once, never changed)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DATA = ROOT / "data"
FEED_PATH = DATA / "web_feed.json"
STATE_PATH = DATA / "state.json"
CANDIDATES_PATH = DATA / "yt_candidates.json"
CONFIG_PATH = ROOT / "config.json"
PEOPLE_PATH = REPO / "guru_yt" / "channels.json"   # one source of truth for names, tiers, roles
KST = ZoneInfo("Asia/Seoul")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_kst(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


class Http:
    def __init__(self, get: Callable | None = None, post: Callable | None = None):
        self._get, self._post = get, post

    def get(self, url, params=None, headers=None, timeout=25):
        if self._get:
            return self._get(url, params=params, headers=headers)
        r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
        return r.status_code, r.text, r.url

    def post(self, url, data=None, json_body=None, headers=None, timeout=60):
        if self._post:
            return self._post(url, data=data, json_body=json_body, headers=headers)
        r = requests.post(url, data=data, json=json_body, headers={**UA, **(headers or {})}, timeout=timeout)
        return r.status_code, r.text


# ---------- text helpers ----------

def norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def html_text(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", html or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
          .replace("&#8217;", "'").replace("&#8220;", '"').replace("&#8221;", '"'))
    return re.sub(r"\s+", " ", t).strip()


def name_hit(person: dict, text: str) -> bool:
    """Full names only on the web: a bare surname ('Karsan') also matches unrelated companies and people."""
    low = (text or "").lower()
    full = ([n for n in person["names"] if " " in n.strip()] or person["names"]) + person.get("headline_names", [])
    return any(re.search(r"\b" + re.escape(n.lower()) + r"\b", low) for n in full)


def last_names(person: dict) -> list[str]:
    out = {n.split()[-1].lower() for n in person["names"]}
    return sorted(out)


# ---------- discovery ----------

def gnews_search(http: Http, query: str, days: int) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    status, text, _ = http.get(url)
    if status != 200:
        raise RuntimeError(f"gnews {status}")
    root = ET.fromstring(text.encode())
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        src = it.findtext("source") or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            continue
        out.append({"title": title, "outlet": src, "published_at": pub.isoformat(), "gnews_link": it.findtext("link") or ""})
    return out


def gnews_decode(http: Http, link: str) -> str | None:
    """Resolve a news.google.com/rss/articles/... link to the publisher URL."""
    if "/articles/" not in link:
        return None
    aid = link.split("/articles/")[1].split("?")[0]
    status, page, _ = http.get(f"https://news.google.com/articles/{aid}")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page or "")
    ts = re.search(r'data-n-a-ts="([^"]+)"', page or "")
    if status != 200 or not (sg and ts):
        return None
    inner = (f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]')
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
    status, text = http.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
                             headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    m = re.search(r'\\"garturlres\\",\\"(https?://[^\\"]+)', text or "")
    return m.group(1).encode().decode("unicode_escape") if m else None


def is_video_page(url: str) -> bool:
    u = url.lower()
    return any(k in u for k in ("cnbc.com/video/", "finance.yahoo.com/video", "/videos/", "bloomberg.com/news/videos"))


def youtube_search(http: Http, q: str) -> list[dict]:
    status, html, _ = http.get("https://www.youtube.com/results", params={"search_query": q})
    if status != 200:
        return []
    out, seen = [], set()
    for vid, title in re.findall(r'"videoId":"([\w-]{11})".{0,400}?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', html):
        if vid in seen:
            continue
        seen.add(vid)
        try:
            title = json.loads(f'"{title}"')
        except Exception:
            pass
        out.append({"video_id": vid, "title": title})
        if len(out) >= 3:
            break
    return out


def title_similar(a: str, b: str) -> float:
    from rapidfuzz import fuzz
    return fuzz.token_set_ratio(norm(a), norm(b))


# ---------- gemini (light model, quote proposals only) ----------

QUOTE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "quotes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "text": {"type": "STRING"}, "attribution": {"type": "STRING"}}, "required": ["text", "attribution"]}},
        "direction": {"type": "STRING", "enum": ["bullish", "neutral", "bearish", "mixed", "not_stated"]},
    },
    "required": ["quotes", "direction"],
}


class GeminiQuota(Exception):
    pass


def gemini_quotes(http: Http, key: str, models: list[str], person: str, text: str, kind: str, sleep: Callable):
    prompt = (f"Tracked person: {person}.\n"
              f"Below is the text of {'an article' if kind == 'article' else 'a transcript or post by the person'}.\n"
              "List up to 6 passages that are the tracked person's OWN words about markets, the economy, rates, "
              "stocks, credit or positioning. Copy each passage exactly as it appears in the text, character for "
              "character, without fixing or shortening words inside it. Only use words inside quotation marks that "
              "the text attributes to this person" + (", or sentences the person wrote/spoke themselves" if kind != "article" else "")
              + ". Never use the reporter's paraphrase or another person's words. attribution = the words in the "
              "text that show who said it (e.g. 'said Newton').\n"
              "direction = this person's own view on US equities from those words only.\n\nTEXT:\n" + text[:30000])
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": QUOTE_SCHEMA}}
    last = None
    for model in models:
        for attempt in range(2):
            status, out = http.post(f"{GEMINI_BASE}/{model}:generateContent", json_body=body,
                                    headers={"x-goog-api-key": key, "Content-Type": "application/json"})
            if status == 200:
                try:
                    j = json.loads(out)
                    txt = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])
                    return json.loads(txt), model
                except Exception:
                    last = "bad response"
                    break
            if status == 429:
                last = "quota"
                break  # this model's quota is gone; try the next model
            if status in (500, 502, 503, 504) and attempt == 0:
                sleep(15)
                continue
            last = f"http {status}"
            break
    if last == "quota":
        raise GeminiQuota()
    raise RuntimeError(last or "no model")


def verify_quotes(person: dict, proposals: list[dict], text: str, kind: str) -> list[dict]:
    """Code check, Gemini's say-so is never enough.
    - article: the proposal must match a passage that is actually inside quotation marks in the article
      (fuzzy >= 95), and the person's last name must appear within 400 characters of that passage.
    - own blog / transcript: the proposal must appear in the text (fuzzy >= 95)."""
    from rapidfuzz import fuzz
    names = last_names(person)
    out = []
    if kind == "article":
        segs = [(m.start(), m.group(1)) for m in re.finditer(r'["\u201c]([^"\u201d]{20,1200})["\u201d]', text)]
        for q in proposals or []:
            qt = norm(q.get("text", ""))
            if len(qt) < 25:
                continue
            best, pos = 0.0, None
            for start, seg in segs:
                sc = fuzz.partial_ratio(qt, norm(seg)) if len(qt) <= len(norm(seg)) + 10 else 0
                if sc > best:
                    best, pos = sc, start
            if best < 95 or pos is None:
                continue
            window = text[max(0, pos - 400): pos + 400].lower()
            if not any(re.search(r"\b" + re.escape(n) + r"\b", window) for n in names):
                continue
            out.append({"text": q["text"].strip()[:600], "quote_match": round(best, 1), "speaker_verified": True,
                        "attribution": str(q.get("attribution", ""))[:120]})
        return out
    ntext = norm(text)
    for q in proposals or []:
        qt = norm(q.get("text", ""))
        if len(qt) < 25:
            continue
        al = fuzz.partial_ratio_alignment(qt, ntext)
        if al.score < 95:
            continue
        out.append({"text": q["text"].strip()[:600], "quote_match": round(al.score, 1), "speaker_verified": True,
                    "attribution": str(q.get("attribution", ""))[:120]})
    return out


# ---------- official sources ----------

CARSON_FEEDS = [
    "https://www.carsongroup.com/feed",
    "https://www.carsongroup.com/feed/",
    "https://www.carsongroup.com/insights/blog/feed/",
    "https://www.carsongroup.com/author/ryan-detrick/feed/",
    "https://carsongroup.com/feed/",
]


def carson_detrick(http: Http, since: datetime) -> list[dict]:
    """Carson Group RSS. The main feed URL started answering 403 to Actions on 10/26; try the other public
    feed URLs WordPress serves before giving up. Records every attempt's status in the error."""
    tried, text = [], None
    for url in CARSON_FEEDS:
        try:
            status, body, _ = http.get(url, headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
        except Exception as e:
            tried.append(f"{url.split('carsongroup.com')[-1] or '/'}:{type(e).__name__}")
            continue
        tried.append(f"{url.split('carsongroup.com')[-1] or '/'}:{status}")
        if status == 200 and "<rss" in body[:500].lower():
            text = body
            break
    if text is None:
        raise RuntimeError("carson " + " ".join(tried))
    root = ET.fromstring(text.encode())
    out = []
    for it in root.findall(".//item"):
        if (it.findtext("{http://purl.org/dc/elements/1.1/}creator") or "").strip().lower() != "ryan detrick":
            continue
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            continue
        if pub < since:
            continue
        body = it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or ""
        out.append({"title": (it.findtext("title") or "").strip(), "url": it.findtext("link") or "",
                    "published_at": pub.isoformat(), "text": html_text(body)})
    return out


def wilson_episodes(http: Http, since: datetime) -> list[dict]:
    """Morgan Stanley 'Thoughts on the Market' contributor page for Mike Wilson; episodes carry transcripts."""
    status, html, _ = http.get("https://www.morganstanley.com/ideas/thoughts-on-the-market-wilson.html")
    if status != 200:
        raise RuntimeError(f"ms {status}")
    links = sorted(set(re.findall(r'href="(/insights/podcasts/thoughts-on-the-market/[^"#?]+)"', html)
                       + re.findall(r'href="(/ideas/podcast-[^"#?]+)"', html)))
    out = []
    for path in links[-12:]:
        status, page, _ = http.get("https://www.morganstanley.com" + path)
        if status != 200:
            continue
        m = (re.search(r'"datePublished"\s*:\s*"([^"]+)"', page) or re.search(r'<meta[^>]+article:published_time[^>]+content="([^"]+)"', page)
             or re.search(r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, 20\d\d)\b', page))
        if not m:
            continue
        try:
            raw = m.group(1)
            pub = (parse_ts(raw) if raw[:4].isdigit() else datetime.strptime(raw.replace("Sept", "Sep")[:3] + raw[raw.index(" "):], "%b %d, %Y")
                   .replace(tzinfo=timezone.utc))
        except Exception:
            continue
        if pub < since:
            continue
        text = html_text(page)
        if "wilson" not in text.lower():
            continue
        title = re.search(r"<title>(.*?)</title>", page, re.S)
        out.append({"title": html_text(title.group(1)) if title else path, "url": "https://www.morganstanley.com" + path,
                    "published_at": pub.isoformat(), "text": text})
    return out


# ---------- main ----------

def run(gemini_key: str | None, http: Http | None = None, now: datetime | None = None,
        sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> dict:
    http = http or Http()
    now = now or now_utc()
    cfg = load_json(CONFIG_PATH, {})
    people = load_json(PEOPLE_PATH, {})["people"]
    by_name = {p["person"]: p for p in people}
    window = timedelta(days=cfg.get("window_days", 7))
    days = cfg.get("discovery_days", 3)
    fetched_at = iso_kst(now)
    state = load_json(STATE_PATH, {})
    old = load_json(FEED_PATH, {})
    items = {i["canonical_url"]: i for i in old.get("items", [])
             # drop news items whose headline no longer passes the full-name filter (e.g. 'Karsan Otomotiv')
             if i.get("source_kind") not in ("news_article", "video_page")
             or (i["person"] in by_name and name_hit(by_name[i["person"]], i["title"]))}
    gem_day = state.get("gemini") or {}
    if gem_day.get("date") != now.strftime("%Y-%m-%d"):
        gem_day = {"date": now.strftime("%Y-%m-%d"), "calls": 0}
    gem_cap = cfg.get("gemini_daily_cap", 10)
    models = cfg.get("gemini_models", ["models/gemini-3.5-flash-lite", "models/gemini-flash-lite-latest",
                                       "models/gemini-3.1-flash-lite"])
    gem_ok = bool(gemini_key)
    errors, stats = {}, {"discovered": 0, "new_items": 0, "decoded": 0, "bodies": 0, "quotes_verified": 0,
                         "video_candidates": 0, "gemini_calls": 0, "gemini_skipped_cap": 0}
    t0 = clock()
    budget = cfg.get("time_budget_sec", 900)
    candidates = load_json(CANDIDATES_PATH, {"videos": []})
    cand_ids = {c["video_id"] for c in candidates["videos"]}

    def new_item(person: dict, kind: str, title: str, outlet: str, url: str, pub: str) -> dict:
        return {"person": person["person"], "tier": person["tier"], "role": person.get("role", "core"),
                "source_kind": kind, "title": title[:300], "outlet": outlet, "published_at": pub,
                "discovered_at": fetched_at, "article_url": url, "canonical_url": url.split("#")[0],
                "body_fetch_status": None, "direct_quotes": [], "direction_hint": None,
                "evidence_grade": "제목·설명만 확인", "evidence_ready_at": None, "gemini_status": "pending",
                "gemini_error_count": 0}

    # 1) news discovery, T1 -> T2 -> T3 -> non-core
    order = sorted(people, key=lambda p: (p["tier"], p["person"]))
    pstat = {p["person"]: {"person": p["person"], "tier": p["tier"], "role": p.get("role", "core"),
                           "discovery_ok": True, "error": None} for p in order}
    complete = {"discovery": True, "processing": True}
    queue: list[tuple[dict, dict]] = []
    for p in order:
        q = " OR ".join(f'"{n}"' for n in [n for n in p["names"] if " " in n.strip()] + p.get("headline_names", [])) + (f" {p['web_query_extra']}" if p.get("web_query_extra") else "")
        try:
            found = gnews_search(http, q, days)
        except Exception as e:
            errors[p["person"]] = f"google news 실패 ({e})"
            pstat[p["person"]].update(discovery_ok=False, error=f"google news 실패 ({str(e)[:80]})")
            continue
        stats["discovered"] += len(found)
        for f in found[: cfg.get("max_items_per_person", 6)]:
            if not name_hit(p, f["title"]):
                continue  # noise filter: the person must be named in the headline
            queue.append((p, f))
        sleep(1)

    seen_titles: dict[str, str] = {}
    for p, f in queue:
        if clock() - t0 > budget:
            complete["discovery"] = False
            break
        key = (p["person"], norm(f["title"])[:80])
        if key in seen_titles:  # same story syndicated by several outlets
            continue
        url = None
        for it in items.values():
            if it["person"] == p["person"] and norm(it["title"])[:80] == key[1]:
                url = it["canonical_url"]
        if url:
            seen_titles[key] = url
            continue
        url = gnews_decode(http, f["gnews_link"])
        sleep(1)
        if not url:
            continue
        stats["decoded"] += 1
        seen_titles[key] = url
        if url in items:
            continue
        kind = "video_page" if is_video_page(url) else "news_article"
        items[url] = new_item(p, kind, f["title"], f["outlet"], url, f["published_at"])
        stats["new_items"] += 1

    # 2) official sources
    since = now - timedelta(days=days)
    if "Ryan Detrick" in by_name:
        try:
            for post in carson_detrick(http, since):
                if post["url"] not in items:
                    it = new_item(by_name["Ryan Detrick"], "official_blog", post["title"], "Carson Group", post["url"], post["published_at"])
                    it["_text"] = post["text"]
                    items[post["url"]] = it
                elif items[post["url"]]["gemini_status"] in ("pending", "error"):
                    items[post["url"]]["_text"] = post["text"]
            pstat["Ryan Detrick"]["official_source_ok"] = True
        except Exception as e:
            errors["Carson Group feed"] = str(e)[:120]
            pstat["Ryan Detrick"]["official_source_ok"] = False
    if "Mike Wilson" in by_name:
        try:
            for ep in wilson_episodes(http, since):
                if ep["url"] not in items:
                    it = new_item(by_name["Mike Wilson"], "official_transcript", ep["title"], "Morgan Stanley", ep["url"], ep["published_at"])
                    it["_text"] = ep["text"]
                    items[ep["url"]] = it
                elif items[ep["url"]]["gemini_status"] in ("pending", "error"):
                    items[ep["url"]]["_text"] = ep["text"]
            pstat["Mike Wilson"]["official_source_ok"] = True
        except Exception as e:
            errors["Morgan Stanley podcast"] = str(e)[:120]
            pstat["Mike Wilson"]["official_source_ok"] = False

    # 3) process: video bridge, then bodies + quotes (T1 first, newest first, capped)
    todo = [i for i in items.values() if i["gemini_status"] in ("pending", "error")
            and now - parse_ts(i["published_at"]) <= window]
    todo.sort(key=lambda i: (i["tier"], 0 if i["source_kind"] != "video_page" else 1, -parse_ts(i["published_at"]).timestamp()))
    for it in todo:
        if clock() - t0 > budget:
            complete["processing"] = False
            break
        person = by_name.get(it["person"])
        if not person:
            continue
        if it["source_kind"] == "video_page":
            if not it.get("yt_searched"):
                it["yt_searched"] = True
                for r in youtube_search(http, it["title"]):
                    if title_similar(r["title"], it["title"]) >= 80 and r["video_id"] not in cand_ids:
                        candidates["videos"].append({"video_id": r["video_id"], "person": it["person"],
                                                     "source": "web_discovery", "article_url": it["canonical_url"],
                                                     "article_date": it["published_at"], "title_hint": r["title"],
                                                     "added_at": fetched_at})
                        cand_ids.add(r["video_id"])
                        stats["video_candidates"] += 1
                sleep(1)
        # body text
        text = it.pop("_text", None)
        if text is None:
            status, html, _ = http.get(it["article_url"])
            it["body_fetch_status"] = status
            if status != 200:
                it["gemini_status"] = "no_body"
                continue
            text = html_text(html)
        else:
            it["body_fetch_status"] = 200
        stats["bodies"] += 1
        if it["source_kind"] in ("news_article", "video_page") and not name_hit(person, text):
            it["gemini_status"] = "not_about_person"
            continue
        if it["source_kind"] in ("news_article", "video_page") and not re.search(r'["“][^"”]{25,}["”]', text):
            it["gemini_status"] = "no_direct_quotes"  # nothing in quotation marks: cannot be 1차-인용
            continue
        if not gem_ok or gem_day["calls"] >= gem_cap:
            stats["gemini_skipped_cap"] += 1
            continue
        try:
            gem_day["calls"] += 1
            stats["gemini_calls"] += 1
            kind = "article" if it["source_kind"] in ("news_article", "video_page") else "own"
            res, model = gemini_quotes(http, gemini_key, models, it["person"], text, kind, sleep)
        except GeminiQuota:
            gem_ok = False
            it["gemini_last_error"] = "quota (429) on every light model"
            continue
        except Exception as e:
            it["gemini_error_count"] += 1
            it["gemini_last_error"] = str(e)[:120]
            it["gemini_status"] = "unavailable" if it["gemini_error_count"] >= 3 else "error"
            continue
        quotes = verify_quotes(person, res.get("quotes"), text, kind)
        it["gemini_status"], it["gemini_model"] = "ready", model
        it["direct_quotes"] = quotes
        it["direction_hint"] = res.get("direction") if quotes else None
        stats["quotes_verified"] += len(quotes)
        if quotes:
            it["evidence_grade"] = "1차" if it["source_kind"] in ("official_blog", "official_transcript") else "1차-인용"
            if not it.get("evidence_ready_at"):
                it["evidence_ready_at"] = fetched_at
        sleep(2)

    for it in items.values():
        it.pop("_text", None)
    keep = [i for i in items.values() if now - parse_ts(i["published_at"]) <= window]
    keep.sort(key=lambda i: i["published_at"], reverse=True)
    candidates["videos"] = [c for c in candidates["videos"] if now - parse_ts(c["added_at"]) <= window]
    state["gemini"] = gem_day
    pending_left = [i for i in keep if i["gemini_status"] in ("pending", "error")]
    for st in pstat.values():
        mine = [i for i in keep if i["person"] == st["person"]]
        st["items_in_window"] = len(mine)
        st["scoring_items"] = sum(1 for i in mine if i["evidence_grade"] in ("1차", "1차-인용"))
        st["pending_items"] = sum(1 for i in mine if i in pending_left)
    feed = {
        "schema": "guru-web/v1",
        "generated_at": fetched_at,
        "data_ok": not errors,
        "errors": errors,
        "source": "Google News discovery (publisher URL decoded), Carson Group feed (author Ryan Detrick), Morgan Stanley "
                  "Thoughts on the Market (Mike Wilson). direct_quotes are proposed by a light Gemini model and kept only "
                  "if code finds them in the source text (quote_match >= 95) and, for articles, near the person's name. "
                  "News quotes = 1차-인용, the person's own blog/transcript = 1차. direction_hint is Gemini's reading, not "
                  "evidence. Article bodies are never stored. Video pages are handed to guru_yt via yt_candidates.json.",
        "new_statement_clock": "evidence_ready_at",
        "discovery_complete": complete["discovery"],
        "processing_complete": complete["processing"] and not any(
            i["gemini_status"] == "pending" for i in pending_left),
        "people": list(pstat.values()),
        "stats": stats,
        "gemini": {"calls_today": gem_day["calls"], "daily_cap": gem_cap, "models": models,
                   "status": "ok" if gem_ok else ("no key" if not gemini_key else "quota")},
        "items": keep,
    }
    write_json(FEED_PATH, feed)
    write_json(STATE_PATH, state)
    write_json(CANDIDATES_PATH, candidates)
    return feed


def main() -> int:
    feed = run(os.environ.get("GEMINI_API_KEY") or None)
    print(json.dumps({k: feed[k] for k in ("generated_at", "data_ok", "errors", "stats", "gemini")}, ensure_ascii=False, indent=2))
    for i in feed["items"][:40]:
        print(f"{i['person']:<14} {i['source_kind']:<19} {i['evidence_grade']:<10} {i['gemini_status']:<16} "
              f"{len(i['direct_quotes'])}q | {i['outlet'][:18]} | {i['title'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
