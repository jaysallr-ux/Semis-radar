"""guru_flow: SHADOW collector for flow strategists (first: Charlie McElligott, Nomura).

Shadow means
- Nothing in the Macro Guru canon reads this folder: not guru_web, guru_yt, guru_x, guru_manifest, the GPT report
  or the Claude audits. It never touches a T1/T2 stance and raises no alert.
- It runs on its own workflow and writes only guru_flow/data/.

What it does
1. Google News RSS for each tracked flow strategist (no YouTube, no X).
2. Resolves the publisher URL and reads only the article's own paragraphs (<p> inside <article> when there is
   one), skipping nav, asides, footers, related-story lists, newsletter and ad blocks.
3. Sorts every kept sentence into one of three kinds. Article bodies are never stored, only short sentences.
   - attributed: the person's own statement, i.e. the sentence names him next to a speech verb
     ("McElligott wrote", "said Nomura's Charlie McElligott", "according to McElligott").
   - context: the sentence right after an attributed one, kept only when it carries a flow topic or a condition
     ("Below that level the selling would accelerate"). It is never counted as his statement.
   - mention: the name appears without a speech verb. Kept for reference, never counted as his statement.
4. Dedupes by URL, by close titles within a time window, and by the same attributed sentence quoted by several
   outlets, but never merges two texts whose direction (buy/sell, up/down), concrete numbers, or (for quotes)
   condition differ, however similar the wording. Every unique note is kept; rewrites become extra sources.
5. Retries sources whose body could not be read (title only, paywall, fetch error) on later runs, a few times,
   while the article is fresh.
6. Tags topics (CTA, dealer gamma, vol control, opex), conditional cues and concrete levels/amounts (money,
   6,400, and comma-less index levels such as SPX 6400 with level context; years are not levels). It never
   decides direction.
7. Writes a capped digest for a future report (the cap limits what a report would read, not what is stored) and
   the three shadow-period metrics: body read rate, own-statement rate, notes with concrete levels/amounts.

Judging rule carried in the data
- judgable=true only when at least one attributed sentence exists. Title only, mention only, or context only:
  reference, never judged.
- A conditional cue means a trigger level, not a flow happening now.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
NOTES_PATH = DATA / "notes.json"
DIGEST_PATH = DATA / "digest.json"
RUNS_PATH = DATA / "runs.csv"
KST = ZoneInfo("Asia/Seoul")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}

TOPICS = {
    "cta": r"\bCTAs?\b|trend[- ]follow|systematic trend|commodity trading advisor",
    "dealer_gamma": r"\bgamma\b|dealer (?:hedg|position)|options? dealers?",
    "vol_control": r"vol(?:atility)?[- ]control|vol(?:atility)?[- ]target|risk[- ]parity|leveraged ETF",
    "opex": r"\bopex\b|options? expir|quad[- ]witch|triple[- ]witch",
}
CONDITIONAL = (r"\bif\b|\bunless\b|\bbelow\b|\babove\b|\bbreak(?:s|ing)?\b|\btrigger|\bthreshold|\bwould\b|\bcould\b"
               r"|\bin the event\b|\bflip(?:s|ping)? level")
LEVELS = (r"\$\s?\d[\d,.]*\s?(?:billion|bn|million|mn|b|m)?\b|\b\d[\d,.]*\s?(?:billion|bn|million|mn)\b"
          r"|\b\d{1,2},\d{3}(?:\.\d+)?\b")
SPEECH = (r"said|says|say|wrote|writes|write|noted|notes|added|adds|warned|warns|explained|explains|estimated"
          r"|estimates|calculated|calculates|expects|expected|told|argued|argues|cautioned|cautions|sees|saw"
          r"|pointed out|points out|flagged|flags|reckons|believes")
OBJECT_BEFORE = (r"(?i:\b(?:follow|follows|followed|following|watch|watches|watched|watching|like|including|than|versus|vs"
                 r"|and|or|with|to|of|about|against|unlike|cite|cites|cited|citing|quote|quotes|quoted|ask|asked|that|"
                 r"said|says|say|told|tells|noted|notes|added|adds|wrote|writes|argued|argues|believe|believes|think|thinks"
                 r"|reported|reports))\s+"
                 r"(?:[A-Z][\w'’.-]*\s+){0,3}$")
RETRYABLE = ("title_only", "paywalled_or_empty", "fetch_error")
SENT_SPLIT = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’]))\s+(?=[A-Z\"“‘(])")


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
        import requests
        r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
        return r.status_code, r.text, r.url

    def post(self, url, data=None, headers=None, timeout=60):
        if self._post:
            return self._post(url, data=data, headers=headers)
        import requests
        r = requests.post(url, data=data, headers={**UA, **(headers or {})}, timeout=timeout)
        return r.status_code, r.text


# ---------- article text ----------

SKIP_TAGS = {"script", "style", "noscript", "nav", "aside", "footer", "header", "form", "figure", "figcaption",
             "svg", "button", "iframe", "template", "select"}
SKIP_ATTR = re.compile(r"related|recommend|more-?stories|read-?more|newsletter|subscribe|promo|sponsor|advert|\bads?\b"
                       r"|ad-|share|social|comment|trending|popular|most-?read|sidebar|footer|breadcrumb|paywall|byline"
                       r"|author-?bio|tags?-list|cookie|signup|outbrain|taboola", re.I)
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}


class _Paras(HTMLParser):
    """Collects <p> text, remembering whether it sat inside <article>, and drops anything inside skipped blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []   # (tag, starts_skip)
        self.skip = 0
        self.article = 0
        self.in_p = 0
        self.buf: list[str] = []
        self.paras: list[tuple[bool, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        a = dict(attrs)
        starts = tag in SKIP_TAGS or bool(SKIP_ATTR.search(" ".join(filter(None, (a.get("class"), a.get("id"),
                                                                                    a.get("role"), a.get("aria-label"))))))
        self.stack.append((tag, starts))
        self.skip += starts
        if tag == "article":
            self.article += 1
        if tag == "p":
            self.in_p += 1
            self.buf = []

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        while self.stack:
            t, starts = self.stack.pop()
            self.skip -= starts
            if t == "article":
                self.article -= 1
            if t == "p":
                self.in_p -= 1
                text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
                if text and self.skip == 0:
                    self.paras.append((self.article > 0, text))
                self.buf = []
            if t == tag:
                break

    def handle_data(self, data):
        if self.in_p and self.skip == 0:
            self.buf.append(data)


def article_text(html: str, min_para_chars: int = 40) -> str:
    p = _Paras()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        return ""
    inside = [t for a, t in p.paras if a and len(t) >= min_para_chars]
    paras = inside or [t for _a, t in p.paras if len(t) >= min_para_chars]
    return " ".join(unescape(t) for t in paras)


# ---------- text ----------

def norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_url(url: str) -> str:
    u = urllib.parse.urlsplit((url or "").split("#")[0])
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
            if not k.lower().startswith(("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "cmpid", "guccounter"))]
    return urllib.parse.urlunsplit((u.scheme, u.netloc.lower(), u.path.rstrip("/"), urllib.parse.urlencode(keep), ""))


def name_hit(person: dict, text: str) -> bool:
    low = (text or "").lower().replace("’", "'")
    return any(re.search(r"\b" + re.escape(n.lower().replace("’", "'")) + r"\b", low)
               for n in person["names"] + person.get("headline_names", []))


def surname_hit(person: dict, text: str) -> bool:
    return any(re.search(r"\b" + re.escape(s) + r"\b", text or "", re.I) for s in person["surnames"])


def attributed(person: dict, sent: str) -> bool:
    """The person's own statement, judged conservatively. Anything uncertain stays 'mention' (reference only).

    Counts only:
    - name as the subject of a speech verb: "McElligott wrote", "Nomura's Charlie McElligott also said",
      "McElligott, Nomura's strategist, said"; not when someone else reports about him
      ("Analysts said McElligott warned...", "strategists like McElligott say").
    - quote inversion after a quote or comma, with nothing after the name but punctuation or an appositive:
      "...," said Nomura's McElligott.  /  ..., said McElligott, a strategist at Nomura.
    - "according to [Nomura's] [Charlie] McElligott".
    Never counts: verb then name inside a clause ("The reporter said McElligott was unavailable",
    "Analysts said Charlie McElligott was wrong"), possessives ("McElligott's model shows").
    """
    for s in person["surnames"]:
        name = r"(?:[A-Z][\w'’.-]*\s+){0,3}" + re.escape(s)           # "Nomura's Charlie McElligott"
        subj = (name + r"(?!['’]s)(?:,[^,.]{1,60},)?(?:\s+(?:also|further|recently|previously|\w+ly)){0,2}\s+(?:"
                + SPEECH + r")\b")
        for m in re.finditer(subj, sent):
            at = sent.find(s, m.start())
            if not re.search(OBJECT_BEFORE, sent[:at]):
                return True
        inversion = (r"(?:[,”\"’]|^)\s*(?:" + SPEECH + r")\s+" + name + r"(?!['’]s)\s*(?:[.!?]|,\s*(?:an?|the)\s|$)")
        if re.search(inversion, sent):
            return True
        if re.search(r"\baccording to\s+" + name + r"(?!['’]s)\b", sent):
            return True
    return False


def topics_of(text: str) -> list[str]:
    return [k for k, rx in TOPICS.items() if re.search(rx, text or "", re.I)]


def conditional_cues(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in re.finditer(CONDITIONAL, text or "", re.I)})


INDEX = r"(?:SPX|SPY|S&P(?:\s?500)?|ES|NDX|NQ|QQQ|Nasdaq(?:[\s-]?100)?|Russell(?:\s?2000)?|RTY|Dow|VIX)"
LEVEL_NUM = r"(\d{3,5}(?:\.\d+)?)"
LEVEL_PREP = r"(?:at|near|around|to|toward|towards|below|above|under|over|through|beneath|of|from)"
LEVEL_PATTERNS = (
    # SPX 6400 / S&P 500 at 6400 / S&P 500 breaks below 6400: an index name right before is strong context
    re.compile(INDEX + r"(?:\s+(?:futures|index))?(?:\s+(?:breaks?|broke|falls?|fell|drops?|dropped|trades?|closes?|"
               r"closed|rises?|rose|rallies|rallied|moves?|holds?|held|sits?))?(?:\s+" + LEVEL_PREP + r")?\s+" + LEVEL_NUM + r"\b"),
    # below 6400 / break 6400 (no index named): needs a level-ish word or sentence punctuation after the number
    re.compile(r"(?i:\b(?:below|above|under|over|through|beneath|breaks?|broke|breaking|near|around|toward|towards))\s+"
               + LEVEL_NUM + r"(?=\s*(?:[.,;:)]|$|(?i:level|strike|area|handle|zone|line|mark|pivot|trigger|on the|in (?:SPX|ES|the index))\b))"),
    # 6400 strike / 6400 level / 6400 gamma flip
    re.compile(LEVEL_NUM + r"\s*(?i:level|strike|handle|area|zone|line|mark|pivot|call|put|gamma)s?\b"),
)


def _is_year(num: str) -> bool:
    return num.isdigit() and len(num) == 4 and 1900 <= int(num) <= 2100


def levels_of(text: str) -> list[str]:
    """Money amounts, comma levels (6,400) and comma-less index levels (SPX 6400) - only with level context, and a
    1900-2100 number counts only right after an index name ('Russell 2000 at 2050'), never as 'in 2026' or
    '2022 levels'."""
    out = {m.group(0).strip() for m in re.finditer(LEVELS, text or "", re.I)}
    for i, rx in enumerate(LEVEL_PATTERNS):
        for m in rx.finditer(text or ""):
            num = m.group(1)
            if len(num.split(".")[0]) < 4:
                continue
            if _is_year(num) and i != 0:
                continue
            if _is_year(num) and i == 0 and not re.search(LEVEL_PREP + r"\s+" + re.escape(num) + r"\b$", m.group(0)):
                continue
            out.add(num)
    return sorted(out)


def classify(person: dict, body: str, max_chars: int) -> list[dict]:
    """Every kept sentence with its kind: attributed / context / mention. Never the whole body."""
    sents = [s.strip() for s in SENT_SPLIT.split(body or "") if 20 <= len(s.strip()) <= 1200]
    out, seen = [], set()

    def add(i, kind):
        s = sents[i]
        s = s if len(s) <= max_chars else s[: max_chars - 1].rstrip() + "…"
        if s not in seen:
            seen.add(s)
            out.append({"kind": kind, "text": s})

    for i, s in enumerate(sents):
        if not surname_hit(person, s):
            continue
        if attributed(person, s):
            add(i, "attributed")
            nxt = sents[i + 1] if i + 1 < len(sents) else ""
            if nxt and not surname_hit(person, nxt) and (topics_of(nxt) or conditional_cues(nxt)):
                add(i + 1, "context")
        else:
            add(i, "mention")
    return out


def pick_excerpts(all_ex: list[dict], cap: int) -> list[dict]:
    rank = {"attributed": 0, "context": 1, "mention": 2}
    return sorted(all_ex, key=lambda e: rank[e["kind"]])[:cap]


# ---------- google news ----------

def gnews_search(http: Http, query: str, days: int) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    status, text, _ = http.get(url)
    if status != 200:
        raise RuntimeError(f"gnews {status}")
    out = []
    for it in ET.fromstring(text.encode()).findall(".//item"):
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
    if "/articles/" not in (link or ""):
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


# ---------- fetching one source ----------

def fetch_source(http: Http, person: dict, src: dict, now: datetime, cfg: dict) -> None:
    """Fills src in place: url, fetch_url, body_status, excerpts, attempts, last_attempt_at."""
    src["attempts"] = src.get("attempts", 0) + 1
    src["last_attempt_at"] = iso_kst(now)
    raw = src.get("fetch_url")
    if not raw:
        try:
            raw = gnews_decode(http, src["gnews_link"])
        except Exception:
            raw = None
        if not raw:
            src["body_status"] = "title_only"
            return
        src["fetch_url"], src["url"] = raw, clean_url(raw)
    try:
        st, html, _ = http.get(raw)
    except Exception:
        src["body_status"] = "fetch_error"
        return
    body = article_text(html, cfg["min_para_chars"]) if st == 200 else ""
    if len(body) < cfg["min_body_chars"]:
        src["body_status"] = "paywalled_or_empty"
        return
    if not surname_hit(person, body):
        src["body_status"] = "no_person_in_text"
        src["excerpts"] = []
        return
    ex = classify(person, body, cfg["max_excerpt_chars"])
    src["excerpts"] = ex
    src["body_status"] = "read" if any(e["kind"] == "attributed" for e in ex) else "mention_only"


def retry_due(src: dict, now: datetime, cfg: dict) -> bool:
    if src.get("body_status") not in RETRYABLE or src.get("attempts", 0) >= cfg["max_attempts"]:
        return False
    if now - parse_ts(src["published_at"]) > timedelta(days=cfg["retry_days"]):
        return False
    last = src.get("last_attempt_at")
    return not last or now - parse_ts(last) >= timedelta(hours=cfg["retry_gap_hours"])


# ---------- notes ----------

def title_sim(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / max(1, len(ta | tb))
    return max(jac, SequenceMatcher(None, na, nb).ratio())


DIRECTION = {
    "buy": r"\b(?:buy|buys|buying|bought|buyers?|purchas\w*|inflows?|demand|re-?lever\w*|short[- ]covering|chas(?:e|ing))\b",
    "sell": r"\b(?:sell|sells|selling|sold|sellers?|dump\w*|supply|outflows?|de-?lever\w*|de-?risk\w*|liquidat\w*|unwind\w*)\b",
    "up": r"\b(?:rally|rallies|rallying|rallied|rise|rises|rising|rose|higher|upside|gains?|surg\w*|jump\w*|squeeze)\b",
    "down": r"\b(?:fall|falls|falling|fell|drop\w*|declin\w*|lower|downside|slump\w*|plung\w*|crash\w*|tumbl\w*)\b",
}
OPPOSITE = (("buy", "sell"), ("up", "down"))


def directions(text: str) -> set[str]:
    return {k for k, rx in DIRECTION.items() if re.search(rx, text or "", re.I)}


def norm_level(x: str) -> str:
    """'$50 billion' == '$50bn' == '50 billion', '6,400' == '6400'."""
    x = x.lower().replace(",", "").replace("$", "").replace(" ", "")
    return re.sub(r"(billion|bn|b)$", "b", re.sub(r"(million|mn|m)$", "m", x))


def conflict(a: str, b: str, with_condition: bool = False) -> str | None:
    """Why two texts must not be the same note, or None. Similar wording is not enough when the direction
    (buy/sell, up/down), the concrete numbers, or (for quotes) the presence of a condition differ."""
    da, db = directions(a), directions(b)
    for x, y in OPPOSITE:
        if (x in da and y not in da and y in db and x not in db) or (y in da and x not in da and x in db and y not in db):
            return f"direction {x}/{y}"
    la, lb = {norm_level(x) for x in levels_of(a)}, {norm_level(x) for x in levels_of(b)}
    if la and lb and not la & lb:
        return "numbers"
    if with_condition and bool(conditional_cues(a)) != bool(conditional_cues(b)):
        return "condition"
    return None


def same_note(note: dict, src: dict, cfg: dict) -> bool:
    """Same URL is always the same note. Otherwise, within the window: close titles or the same attributed
    sentence, and no conflict in direction, numbers or (for quotes) condition with ANY source already in the note.
    Shared numbers or topics alone never merge. When in doubt, keep two notes (a duplicate is cheap, a wrong merge
    that flips buy into sell is not)."""
    if src.get("url") and src["url"] in {s.get("url") for s in note["sources"]}:
        return True
    if abs(parse_ts(note["published_at"]) - parse_ts(src["published_at"])) > timedelta(hours=cfg["same_note_hours"]):
        return False
    if any(conflict(s["title"], src["title"]) for s in note["sources"]):
        return False
    mine = [e["text"] for s in note["sources"] for e in s.get("excerpts", []) if e["kind"] == "attributed"]
    theirs = [e["text"] for e in src.get("excerpts", []) if e["kind"] == "attributed"]
    if any(conflict(a, b, with_condition=True) for a in mine for b in theirs
           if SequenceMatcher(None, norm(a), norm(b)).ratio() >= cfg["same_note_title_sim"]):
        return False
    if max(title_sim(s["title"], src["title"]) for s in note["sources"]) >= cfg["same_note_title_sim"]:
        return True
    return any(SequenceMatcher(None, norm(a), norm(b)).ratio() >= cfg["same_quote_sim"] for a in mine for b in theirs)


def refresh(note: dict, cfg: dict) -> None:
    all_ex, seen = [], set()
    for s in note["sources"]:
        for e in s.get("excerpts", []):
            k = norm(e["text"])
            if k not in seen:
                seen.add(k)
                all_ex.append(e)
    note["excerpts"] = pick_excerpts(all_ex, cfg["max_excerpts"])
    own = [e["text"] for e in note["excerpts"] if e["kind"] in ("attributed", "context")]
    titles = " ".join(s["title"] for s in note["sources"])
    note["topics"] = topics_of(titles + " " + " ".join(e["text"] for e in note["excerpts"]))
    note["conditional_cues"] = conditional_cues(" ".join(own))
    note["levels"] = levels_of(" ".join(own))
    note["own_statement"] = any(e["kind"] == "attributed" for e in note["excerpts"])
    note["body_read"] = any(s.get("body_status") in ("read", "mention_only", "no_person_in_text") for s in note["sources"])
    note["judgable"] = note["own_statement"]
    if note["judgable"]:
        note["judging_note"] = ("본인 발언 확인. " + ("조건부 표현 있음: 발동 가격 기준이지 지금 나오는 물량 아님."
                                                 if note["conditional_cues"] else "조건부 표현 없음."))
    elif note["body_read"]:
        note["judging_note"] = "본문은 읽었지만 본인 발언 미확인(이름 언급뿐): 참고용, 판정 금지."
    else:
        note["judging_note"] = "제목만 확보: 참고용, 판정 금지. 재수집 대기 중일 수 있음."


def attach(notes: list[dict], person: dict, src: dict, now: datetime, cfg: dict) -> str:
    target = next((n for n in notes if n["person"] == person["person"] and same_note(n, src, cfg)), None)
    if target:
        target["sources"].append(src)
        refresh(target, cfg)
        return "merged"
    note = {"note_id": f"{person['surnames'][0].lower()}-{parse_ts(src['published_at']).astimezone(KST):%Y%m%d}-"
                       f"{hashlib.sha1(src['gnews_link'].encode()).hexdigest()[:8]}",
            "person": person["person"], "class": "flow_watch", "published_at": src["published_at"],
            "first_seen_at": iso_kst(now), "headline": src["title"], "sources": [src]}
    refresh(note, cfg)
    notes.append(note)
    return "new"


def metrics(notes: list[dict]) -> dict:
    srcs = [s for n in notes for s in n["sources"]]
    tried = [s for s in srcs if s.get("attempts")]
    read = [s for s in srcs if s.get("body_status") in ("read", "mention_only", "no_person_in_text")]
    own = [n for n in notes if n["own_statement"]]
    lv = [n for n in own if n["levels"]]
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    return {"notes": len(notes), "sources": len(srcs), "sources_tried": len(tried), "body_read": len(read),
            "body_read_rate_pct": pct(len(read), len(tried)),
            "own_statement_notes": len(own), "own_statement_rate_pct": pct(len(own), len(notes)),
            "own_statement_with_levels": len(lv),
            "status_counts": {k: sum(1 for s in srcs if s.get("body_status") == k)
                              for k in sorted({s.get("body_status") for s in srcs})}}


# ---------- run ----------

def run(http: Http | None = None, now: datetime | None = None, cfg: dict | None = None) -> dict:
    http = http or Http()
    now = now or now_utc()
    cfg = cfg or load_json(CONFIG_PATH, {})
    people = {p["person"]: p for p in cfg["people"]}
    notes: list[dict] = load_json(NOTES_PATH, {}).get("notes", [])
    stats = {"found": 0, "new_sources": 0, "new_notes": 0, "merged": 0, "retried": 0, "retry_recovered": 0, "errors": {}}

    # 1. retry sources whose body was not read yet. A recovered source may turn out to quote the same sentence as
    #    another note; it stays where it is (notes are never re-split or re-merged after the fact).
    budget = cfg["max_retry_per_run"]
    for n in notes:
        p = people.get(n["person"])
        if not p:
            continue
        changed = False
        for s in n["sources"]:
            if budget <= 0 or not retry_due(s, now, cfg):
                continue
            budget -= 1
            stats["retried"] += 1
            fetch_source(http, p, s, now, cfg)
            stats["retry_recovered"] += s["body_status"] not in RETRYABLE
            changed = True
        if changed:
            refresh(n, cfg)

    # 2. discovery
    seen_links = {s["gnews_link"] for n in notes for s in n["sources"]}
    seen_urls = {s["url"] for n in notes for s in n["sources"] if s.get("url")}
    fetch_left = cfg["max_fetch_per_run"]
    for p in cfg["people"]:
        q = " OR ".join(f'"{x}"' for x in p["names"] + p.get("headline_names", []))
        try:
            found = gnews_search(http, q, cfg["discovery_days"])
        except Exception as e:
            stats["errors"][p["person"]] = f"google news 실패 ({str(e)[:80]})"
            continue
        for f in sorted(found, key=lambda x: x["published_at"]):
            stats["found"] += 1
            if f["gnews_link"] in seen_links:
                continue
            if not (name_hit(p, f["title"]) or surname_hit(p, f["title"])):
                continue
            seen_links.add(f["gnews_link"])
            src = {"title": f["title"], "outlet": f["outlet"], "url": "", "gnews_link": f["gnews_link"],
                   "published_at": f["published_at"], "body_status": "title_only", "attempts": 0, "excerpts": []}
            if fetch_left > 0:
                fetch_left -= 1
                fetch_source(http, p, src, now, cfg)
                if src.get("url") and src["url"] in seen_urls:
                    continue
            if src.get("url"):
                seen_urls.add(src["url"])
            stats["new_sources"] += 1
            stats["new_notes" if attach(notes, p, src, now, cfg) == "new" else "merged"] += 1

    cutoff = now - timedelta(days=cfg["retention_days"])
    notes = sorted((n for n in notes if parse_ts(n["published_at"]) >= cutoff), key=lambda n: n["published_at"])
    m = metrics(notes)
    out = {"schema": "guru-flow/v2", "mode": "shadow",
           "note": "그림자 수집 전용. 정본(Macro Guru 보고서·감사·T1/T2 판정·경보)은 이 파일을 읽지 않는다.",
           "generated_at": iso_kst(now), "data_ok": not stats["errors"], "run_stats": stats, "metrics": m, "notes": notes}
    write_json(NOTES_PATH, out)

    window_lo = now - timedelta(days=cfg["digest_days"])
    recent = sorted((n for n in notes if parse_ts(n["published_at"]) >= window_lo),
                    key=lambda n: (n["judgable"], bool(n["levels"]), n["published_at"]), reverse=True)
    digest = {"schema": "guru-flow-digest/v2", "mode": "shadow", "generated_at": iso_kst(now),
              "cap": cfg["digest_cap"], "notes_in_window": len(recent),
              "dropped_by_cap": max(0, len(recent) - cfg["digest_cap"]),
              "rule": ("judgable=false는 참고용, 판정 금지. kind=attributed만 본인 발언이고 context·mention은 본인 발언 아님. "
                       "conditional_cues가 있으면 조건부 물량."),
              "notes": [{k: n[k] for k in ("note_id", "person", "published_at", "headline", "topics", "levels",
                                            "conditional_cues", "judgable", "judging_note", "excerpts")}
                        | {"sources": len(n["sources"])} for n in recent[: cfg["digest_cap"]]]}
    write_json(DIGEST_PATH, digest)

    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not RUNS_PATH.exists()
    with RUNS_PATH.open("a", encoding="utf-8") as fh:
        if new_file:
            fh.write("run_at,found,new_sources,new_notes,merged,retried,retry_recovered,notes_total,"
                     "body_read_rate_pct,own_statement_rate_pct,own_statement_with_levels,errors\n")
        fh.write(f"{iso_kst(now)},{stats['found']},{stats['new_sources']},{stats['new_notes']},{stats['merged']},"
                 f"{stats['retried']},{stats['retry_recovered']},{m['notes']},{m['body_read_rate_pct']},"
                 f"{m['own_statement_rate_pct']},{m['own_statement_with_levels']},{len(stats['errors'])}\n")
    return out


def main() -> int:
    out = run()
    s, m = out["run_stats"], out["metrics"]
    print(f"found={s['found']} new_sources={s['new_sources']} new_notes={s['new_notes']} merged={s['merged']} "
          f"retried={s['retried']} recovered={s['retry_recovered']} | notes={m['notes']} "
          f"body_read%={m['body_read_rate_pct']} own%={m['own_statement_rate_pct']} levels={m['own_statement_with_levels']} "
          f"errors={s['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
