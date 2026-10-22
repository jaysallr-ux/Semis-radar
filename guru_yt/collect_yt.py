"""guru_yt: 허용 채널 RSS 로 추적 인물 영상을 찾고, 자막 서비스로 자막 원문을 받아 youtube_feed.json 에 기록.

역할 분담 (guru_x 와 동일)
- YouTube RSS = 영상 발견 (무료, 키 없음)
- 자막 서비스 = 자막 원문 (youtube-transcript.ai 1순위, Supadata 백업)
- Python = 필터, 우선순위, 한도 관리, 상태 전이
- GPT = 이미 확보된 자막만 읽고 판정

시간 기준
- published_at: 유튜브 업로드 시각
- transcript_ready_at: 자막을 처음 확보한 시각. 최초 성공 때 한 번만 기록하고 절대 갱신하지 않는다.
  GPT 는 이 시각으로 "새 발언" 여부를 판단한다.

자막 원문은 피드가 커지지 않도록 data/transcripts/<video_id>.txt 에 따로 저장한다.
API 키는 로그, 피드, 파일 어디에도 기록하지 않는다.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "channels.json"
DATA_DIR = ROOT / "data"
FEED_PATH = DATA_DIR / "youtube_feed.json"
STATE_PATH = DATA_DIR / "state.json"
RUNS_PATH = DATA_DIR / "runs.csv"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
RAW_BASE = "https://raw.githubusercontent.com/jaysallr-ux/Semis-radar/main/guru_yt/data/transcripts"
KST = ZoneInfo("Asia/Seoul")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "m": "http://search.yahoo.com/mrss/"}

YTA_URL = "https://youtube-transcript.ai/transcript/{vid}.txt"
SUPADATA_URL = "https://api.supadata.ai/v1/transcript"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_kst(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class Http:
    """Thin wrapper so tests can inject fakes. Never logs request headers."""

    def __init__(self, get: Callable | None = None):
        self._get = get or self._requests_get

    @staticmethod
    def _requests_get(url: str, params: dict | None = None, headers: dict | None = None,
                      allow_redirects: bool = True) -> tuple[int, str, dict]:
        r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=60,
                         allow_redirects=allow_redirects)
        return r.status_code, r.text, dict(r.headers)

    def get(self, url, params=None, headers=None, allow_redirects=True):
        return self._get(url, params=params, headers=headers, allow_redirects=allow_redirects)


# ---------- discovery ----------

def resolve_channel_id(http: Http, handles: list[str]) -> tuple[str | None, str]:
    for h in handles:
        try:
            status, text, _ = http.get(f"https://www.youtube.com/@{h}")
        except Exception as e:
            last = f"error {type(e).__name__}"
            continue
        if status != 200:
            last = f"@{h} {status}"
            continue
        m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', text) or re.search(r'channel/(UC[\w-]{22})', text)
        if m:
            return m.group(1), f"@{h}"
        last = f"@{h} no id"
    return None, last if handles else "no handles"


def read_rss(http: Http, channel_id: str) -> list[dict]:
    status, text, _ = http.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id})
    if status != 200:
        raise RuntimeError(f"rss {status}")
    root = ET.fromstring(text)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        group = e.find("m:group", NS)
        desc = group.findtext("m:description", default="", namespaces=NS) if group is not None else ""
        out.append({
            "video_id": vid,
            "title": (e.findtext("a:title", default="", namespaces=NS) or "").strip(),
            "published_at": e.findtext("a:published", default="", namespaces=NS),
            "description": (desc or "").strip(),
        })
    return out


def is_short(http: Http, vid: str) -> bool | None:
    try:
        status, _, _ = http.get(f"https://www.youtube.com/shorts/{vid}", allow_redirects=False)
    except Exception:
        return None
    if status == 200:
        return True
    if 300 <= status < 400:
        return False
    return None


def match_person(people: list[dict], title: str, desc: str) -> tuple[dict | None, str | None]:
    for field, text in (("title", title), ("description", desc)):
        low = text.lower()
        for p in sorted(people, key=lambda x: x["tier"]):
            for n in p["names"]:
                if re.search(r"\b" + re.escape(n.lower()) + r"\b", low):
                    return p, field
    return None, None


# ---------- transcripts ----------

class QuotaExhausted(Exception):
    pass


class KeyInvalid(Exception):
    pass


def strip_yta(md: str) -> str:
    """youtube-transcript.ai markdown: drop header block and trailing footer, keep '## Transcript' body."""
    body = md
    m = re.search(r"^## Transcript\s*$", md, re.M)
    if m:
        body = md[m.end():]
    body = re.split(r"^---\s*$", body, maxsplit=1, flags=re.M)[0]
    return body.strip()


def fetch_yta(http: Http, key: str, vid: str) -> tuple[str | None, str, int | None]:
    """Returns (text or None, outcome, server_remaining). outcome: ready / unavailable / error:<x>."""
    status, text, headers = http.get(YTA_URL.format(vid=vid), headers={"Authorization": f"Bearer {key}"})
    rem = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
    remaining = int(rem) if rem and str(rem).isdigit() else None
    if status == 200:
        body = strip_yta(text)
        return (body, "ready", remaining) if len(body) > 50 else (None, "unavailable", remaining)
    if status == 429:
        raise QuotaExhausted()
    if status == 401:
        raise KeyInvalid()
    if status in (404, 410, 422):
        return None, "unavailable", remaining
    return None, f"error:{status}", remaining


def fetch_supadata(http: Http, key: str, vid: str) -> tuple[str | None, str, str | None]:
    """Returns (text or None, outcome, lang). Native captions only (generated mode costs 2 credits/minute)."""
    params = {"url": f"https://www.youtube.com/watch?v={vid}", "text": "true", "mode": "native", "lang": "en"}
    status, text, _ = http.get(SUPADATA_URL, params=params, headers={"x-api-key": key})
    if status == 202:
        try:
            job = json.loads(text).get("jobId")
        except Exception:
            job = None
        if not job:
            return None, "error:202-nojob", None
        for _ in range(60):
            time.sleep(2)
            status, text, _ = http.get(f"{SUPADATA_URL}/{job}", headers={"x-api-key": key})
            try:
                d = json.loads(text)
            except Exception:
                d = {}
            st = d.get("status")
            if st == "completed":
                status = 200
                text = json.dumps(d.get("result") or d)
                break
            if st == "failed":
                return None, "error:job-failed", None
        else:
            return None, "error:job-timeout", None
    if status == 200:
        try:
            d = json.loads(text)
        except Exception:
            return None, "error:bad-json", None
        content = d.get("content")
        if isinstance(content, list):
            content = " ".join(seg.get("text", "") for seg in content)
        content = (content or "").strip()
        return (content, "ready", d.get("lang")) if len(content) > 50 else (None, "unavailable", d.get("lang"))
    if status == 206:
        return None, "unavailable", None
    if status in (401, 403) and "key" in text.lower():
        raise KeyInvalid()
    if status in (402, 429):
        raise QuotaExhausted()
    if status in (403, 404):
        return None, "unavailable", None
    return None, f"error:{status}", None


def appearance(person: dict, transcript: str) -> tuple[int, str | None]:
    """Count mentions of the person's names/aliases in the transcript; return (count, first excerpt)."""
    low = transcript.lower()
    count, first = 0, None
    for n in person["names"] + person.get("aliases", []):
        for m in re.finditer(r"\b" + re.escape(n.lower()) + r"\b", low):
            count += 1
            if first is None or m.start() < first:
                first = m.start()
    excerpt = transcript[max(0, first - 120): first + 200].replace("\n", " ") if first is not None else None
    return count, excerpt


# ---------- main ----------

def run(yta_key: str | None, supadata_key: str | None, http: Http | None = None, now: datetime | None = None,
        sleep: Callable = time.sleep) -> dict:
    http = http or Http()
    now = now or now_utc()
    cfg = load_json(CONFIG_PATH, {})
    people = cfg["people"]
    by_person = {p["person"]: p for p in people}
    window = timedelta(days=cfg.get("window_days", 7))
    max_age = timedelta(hours=cfg.get("transcript_max_age_hours", 48))
    state = load_json(STATE_PATH, {"channel_ids": {}, "yta": {}, "supadata": {"used_total": 0}})
    old = load_json(FEED_PATH, {})
    videos = {v["video_id"]: v for v in old.get("videos", [])}
    prev_gen = old.get("generated_at")
    errors: dict[str, str] = {}
    fetched_at = iso_kst(now)

    # discovery
    channel_out = []
    for ch in cfg["channels"]:
        label = ch["label"]
        cid = ch.get("channel_id") or state["channel_ids"].get(label)
        how = "config" if ch.get("channel_id") else ("cached" if cid else None)
        if not cid:
            cid, how = resolve_channel_id(http, ch.get("handles", []))
            if cid:
                state["channel_ids"][label] = cid
        info = {"label": label, "channel_id": cid, "resolved_by": how, "rss_ok": False}
        if not cid:
            info["error"] = how
            errors[label] = f"채널 ID 확인 실패 ({how})"
            channel_out.append(info)
            continue
        try:
            entries = read_rss(http, cid)
        except Exception as e:
            info["error"] = str(e)
            errors[label] = f"RSS 실패 ({e})"
            channel_out.append(info)
            continue
        info.update(rss_ok=True, rss_count=len(entries))
        if entries:
            oldest = min(parse_ts(x["published_at"]) for x in entries if x["published_at"])
            info["oldest_rss"] = iso_kst(oldest)
            info["gap_possible"] = bool(prev_gen) and oldest > parse_ts(prev_gen) and len(entries) >= 15
        matched = 0
        for e in entries:
            if not e["published_at"] or now - parse_ts(e["published_at"]) > window:
                continue
            if ch["mode"] == "dedicated":
                p, field = by_person.get(ch["person"]), "dedicated_channel"
            else:
                p, field = match_person(people, e["title"], e["description"])
            if not p:
                continue
            matched += 1
            v = videos.get(e["video_id"])
            if v:
                v.update(title=e["title"], description=e["description"][:1500])
                continue
            videos[e["video_id"]] = {
                "video_id": e["video_id"],
                "url": f"https://www.youtube.com/watch?v={e['video_id']}",
                "channel": label,
                "channel_id": cid,
                "channel_mode": ch["mode"],
                "person": p["person"],
                "tier": p["tier"],
                "match": field,
                "title": e["title"],
                "description": e["description"][:1500],
                "published_at": e["published_at"],
                "first_seen_at": fetched_at,
                "is_short": None,
                "transcript_status": "pending_quota",
                "transcript_provider": None,
                "transcript_ready_at": None,
                "transcript_language": None,
                "transcript_chars": None,
                "transcript_url": None,
                "name_mentions": None,
                "appearance_confirmed": None,
                "appearance_excerpt": None,
                "error_count": 0,
                "last_error": None,
            }
        info["matched"] = matched
        channel_out.append(info)

    # quotas (count ourselves; also respect server remaining)
    today = now.strftime("%Y-%m-%d")
    yta = state.get("yta") or {}
    if yta.get("date") != today:
        yta = {"date": today, "used": 0, "server_remaining": None}
    sup = state.get("supadata") or {"used_total": 0}
    yta_cap, reserve = cfg.get("yta_daily_cap", 7), cfg.get("yta_reserve_for_manual", 3)
    sup_cap, sup_tiers = cfg.get("supadata_total_cap", 90), set(cfg.get("supadata_tiers", [1]))
    yta_ok, sup_ok = bool(yta_key), bool(supadata_key)
    provider_notes = {"youtube-transcript-ai": "no key" if not yta_key else "ok",
                      "supadata": "no key" if not supadata_key else "ok"}

    def yta_available() -> bool:
        if not yta_ok or yta["used"] >= yta_cap:
            return False
        rem = yta.get("server_remaining")
        return rem is None or rem > reserve

    # transcripts: pending/error first, then by tier, then newest
    todo = [v for v in videos.values() if v["transcript_status"] in ("pending_quota", "error")]
    todo.sort(key=lambda v: (0 if v.get("first_seen_at") != fetched_at else 1, v["tier"],
                             -parse_ts(v["published_at"]).timestamp()))
    stats = {"attempted": 0, "ready": 0, "unavailable": 0, "errors": 0, "skipped_short": 0, "expired": 0, "left_pending": 0}
    for v in todo:
        if now - parse_ts(v["published_at"]) > max_age and v["transcript_status"] == "pending_quota" and v["error_count"] == 0 \
                and v.get("first_seen_at") == fetched_at:
            v["transcript_status"] = "skipped_old"
            v["last_error"] = "older than transcript_max_age_hours when first discovered (backfill limit)"
            stats["expired"] += 1
            continue
        if now - parse_ts(v["published_at"]) > window:
            v["transcript_status"] = "unavailable"
            v["last_error"] = "expired (older than window)"
            stats["expired"] += 1
            continue
        if cfg.get("skip_shorts", True) and v.get("is_short") is None:
            v["is_short"] = is_short(http, v["video_id"])
        if v.get("is_short"):
            v["transcript_status"] = "skipped_short"
            v["last_error"] = "short (skipped)"
            stats["skipped_short"] += 1
            continue

        text, outcome, provider, lang = None, None, None, None
        if yta_available():
            try:
                stats["attempted"] += 1
                yta["used"] += 1
                text, outcome, rem = fetch_yta(http, yta_key, v["video_id"])
                provider = "youtube-transcript-ai"
                if rem is not None:
                    yta["server_remaining"] = rem
            except QuotaExhausted:
                yta["server_remaining"] = 0
                provider_notes["youtube-transcript-ai"] = "quota exhausted"
            except KeyInvalid:
                yta_ok = False
                provider_notes["youtube-transcript-ai"] = "key invalid (401)"
            except Exception as e:
                outcome, provider = f"error:{type(e).__name__}", "youtube-transcript-ai"
        if outcome is None and sup_ok and v["tier"] in sup_tiers and sup["used_total"] < sup_cap:
            try:
                stats["attempted"] += 1
                sup["used_total"] += 1
                text, outcome, lang = fetch_supadata(http, supadata_key, v["video_id"])
                provider = "supadata"
            except QuotaExhausted:
                sup_ok = False
                provider_notes["supadata"] = "quota exhausted"
            except KeyInvalid:
                sup_ok = False
                provider_notes["supadata"] = "key invalid"
            except Exception as e:
                outcome, provider = f"error:{type(e).__name__}", "supadata"
        if outcome is None:
            v["transcript_status"] = "pending_quota"
            stats["left_pending"] += 1
            continue
        if outcome == "ready":
            TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            (TRANSCRIPT_DIR / f"{v['video_id']}.txt").write_text(text, encoding="utf-8")
            p = by_person[v["person"]]
            cnt, excerpt = appearance(p, text)
            v.update(
                transcript_status="ready",
                transcript_provider=provider,
                transcript_language=lang,
                transcript_chars=len(text),
                transcript_url=f"{RAW_BASE}/{v['video_id']}.txt",
                name_mentions=cnt,
                appearance_confirmed=(v["channel_mode"] == "dedicated") or cnt > 0,
                appearance_excerpt=excerpt,
                last_error=None,
            )
            if not v.get("transcript_ready_at"):
                v["transcript_ready_at"] = fetched_at
            stats["ready"] += 1
        elif outcome == "unavailable":
            v.update(transcript_status="unavailable", transcript_provider=provider, last_error="no transcript")
            stats["unavailable"] += 1
        else:
            v["error_count"] = v.get("error_count", 0) + 1
            v["last_error"] = outcome
            v["transcript_status"] = "unavailable" if v["error_count"] >= cfg.get("max_error_retries", 3) else "error"
            stats["errors"] += 1
        sleep(1)

    # re-evaluate appearance for already-downloaded transcripts (free; picks up alias changes)
    for v in videos.values():
        f = TRANSCRIPT_DIR / f"{v['video_id']}.txt"
        if v["transcript_status"] == "ready" and f.exists() and v["person"] in by_person:
            cnt, excerpt = appearance(by_person[v["person"]], f.read_text(encoding="utf-8"))
            v.update(name_mentions=cnt, appearance_confirmed=(v["channel_mode"] == "dedicated") or cnt > 0,
                     appearance_excerpt=excerpt)

    # prune
    cutoff = now - window
    keep = {k: v for k, v in videos.items() if v.get("published_at") and parse_ts(v["published_at"]) >= cutoff}
    for k in set(videos) - set(keep):
        f = TRANSCRIPT_DIR / f"{k}.txt"
        if f.exists():
            f.unlink()
    vids = sorted(keep.values(), key=lambda v: v["published_at"], reverse=True)

    for name, note in provider_notes.items():
        if note not in ("ok", "no key", "quota exhausted"):
            errors[name] = note
    if not yta_key and not supadata_key:
        errors["_transcripts"] = "자막 키 없음 (YT_TRANSCRIPT_KEY, SUPADATA_KEY)"

    state["yta"], state["supadata"] = yta, sup
    feed = {
        "schema": "guru-yt/v1",
        "generated_at": fetched_at,
        "data_ok": not errors,
        "source": "Discovery via allowlisted channel RSS. Transcripts are verbatim captions from "
                  "youtube-transcript.ai (primary) or Supadata (native captions). Transcript text is in transcript_url.",
        "new_statement_clock": "transcript_ready_at",
        "errors": errors,
        "channels": channel_out,
        "quota": {"yta_used_today": yta["used"], "yta_server_remaining": yta.get("server_remaining"),
                  "yta_daily_cap": yta_cap, "yta_reserve_for_manual": reserve,
                  "supadata_used_total": sup["used_total"], "supadata_total_cap": sup_cap,
                  "providers": provider_notes},
        "stats": stats,
        "videos": vids,
    }
    write_json(STATE_PATH, state)
    write_json(FEED_PATH, feed)
    new_file = not RUNS_PATH.exists()
    with RUNS_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["generated_at", "data_ok", "videos", "ready", "attempted", "yta_used_today",
                        "yta_server_remaining", "supadata_used_total", "errors"])
        w.writerow([fetched_at, feed["data_ok"], len(vids), stats["ready"], stats["attempted"], yta["used"],
                    yta.get("server_remaining"), sup["used_total"], "; ".join(f"{k}: {v}" for k, v in errors.items())])
    return feed


def main() -> int:
    feed = run(os.environ.get("YT_TRANSCRIPT_KEY") or None, os.environ.get("SUPADATA_KEY") or None)
    summary = {k: feed[k] for k in ("generated_at", "data_ok", "errors", "quota", "stats")}
    summary["channels"] = [{k: c.get(k) for k in ("label", "rss_ok", "matched", "error")} for c in feed["channels"]]
    summary["videos"] = [{k: v[k] for k in ("person", "channel", "title", "transcript_status", "appearance_confirmed")}
                         for v in feed["videos"][:20]]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
