"""guru_yt: 허용 채널 RSS 로 추적 인물 영상을 찾고, 자막 원문과 Gemini 영상 직접 분석으로 발언 근거를 youtube_feed.json 에 기록.

역할 분담 (guru_x 와 동일)
- YouTube RSS = 영상 발견 (무료, 키 없음)
- 자막 서비스 = 자막 원문 (youtube-transcript.ai 1순위, Supadata 백업)
- Gemini API = 영상(오디오) 직접 입력으로 본인 출연 여부 + 타임스탬프 인용 추출
- Python = 필터, 우선순위, 한도 관리, 상태 전이, 인용-자막 자동 대조
- GPT = 피드에 확보된 근거만 읽고 판정

근거 등급 (인용 하나하나 따로 매긴다. 영상 단위로 찍지 않는다)
- 1차-A: Gemini 인용이 자막 원문과 caption_match >= 90 으로 대조 통과
- 1차-B: Gemini 가 영상을 직접 분석해 뽑은 인용 (자막 없음 또는 대조 미통과)
- 제목·설명만 확인: Gemini 분석도 자막도 없음

시간 기준
- published_at: 유튜브 업로드 시각
- evidence_ready_at: 채점 가능한 근거(자막 원문 또는 Gemini 본인 발언 확인)를 처음 확보한 시각.
  최초 1회만 기록하고 절대 갱신하지 않는다. 나중에 자막이 들어와 등급이 A 로 올라가도 바뀌지 않는다.
  GPT 는 이 시각으로 "새 발언" 여부를 판단한다.
- transcript_ready_at: 자막을 처음 확보한 시각 (참고용, 역시 최초 1회)

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
LEGACY_TRANSCRIPT_DIR = DATA_DIR / "transcripts"  # old public location, migrated away on first run
TRANSCRIPT_DIR = ROOT / "cache" / "transcripts"    # gitignored; persisted between runs by actions/cache only
QUEUE_PATH = DATA_DIR / "discovered.json"
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

    def post_json(self, url, body, headers=None, timeout=420):
        if hasattr(self, "_post"):
            return self._post(url, body=body, headers=headers, timeout=timeout)
        r = requests.post(url, json=body, headers={"Content-Type": "application/json", **(headers or {})},
                          timeout=timeout)
        return r.status_code, r.text, dict(r.headers)


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


def video_meta(http: Http, vid: str, supadata_key: str | None, state: dict) -> dict:
    """Channel from YouTube oEmbed (works from Actions; the watch page is behind a bot check there) and the
    exact upload date from Supadata metadata (1 credit) when a key is set."""
    out = {}
    try:
        status, text, _ = http.get("https://www.youtube.com/oembed",
                                   params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
        if status == 200:
            j = json.loads(text)
            out.update(title=j.get("title", ""), channel_name=j.get("author_name"), channel_url=j.get("author_url"))
    except Exception:
        pass
    sup = state.setdefault("supadata", {"used_total": 0})
    if out.get("channel_name") and supadata_key and sup.get("used_total", 0) < 90:
        try:
            sup["used_total"] = sup.get("used_total", 0) + 1
            status, text, _ = http.get("https://api.supadata.ai/v1/metadata",
                                       params={"url": f"https://www.youtube.com/watch?v={vid}"},
                                       headers={"x-api-key": supadata_key})
            if status == 200:
                j = json.loads(text)
                media = j.get("media") if isinstance(j.get("media"), dict) else {}
                out["published_at"] = j.get("uploadDate") or j.get("createdAt") or j.get("publishedAt") or media.get("uploadDate")
                ch = j.get("channel") or j.get("author") or {}
                if isinstance(ch, dict):
                    out["channel_id"] = ch.get("id")
                out["description"] = (j.get("description") or "")[:1500]
                dur = j.get("duration") or media.get("duration")
                if isinstance(dur, (int, float)):
                    out["is_short"] = dur <= 60
        except Exception:
            pass
    return out


EXTRA_MATCHES = ("web_discovery", "extra")  # videos accepted one by one: exempt from the 48h backfill limit


def watch_meta(http: Http, vid: str) -> dict:
    """Channel, publish date, title, description and length from the public watch page."""
    try:
        status, text, _ = http.get(f"https://www.youtube.com/watch?v={vid}")
    except Exception:
        return {}
    if status != 200:
        return {}
    def find(pat):
        m = re.search(pat, text)
        return m.group(1) if m else None
    def unesc(x):
        try:
            return json.loads(f'"{x}"') if x is not None else None
        except Exception:
            return x
    pub = find(r'"uploadDate":"([^"]+)"') or find(r'"publishDate":"([^"]+)"')
    if pub and len(pub) == 10:
        pub += "T00:00:00+00:00"
    secs = find(r'"lengthSeconds":"(\d+)"')
    return {"channel_id": find(r'"channelId":"(UC[\w-]{22})"'), "channel_name": unesc(find(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"')),
            "published_at": pub, "title": unesc(find(r'"videoDetails":\{"videoId":"[\w-]{11}","title":"((?:[^"\\]|\\.)*)"')) or "",
            "description": (unesc(find(r'"shortDescription":"((?:[^"\\]|\\.)*)"')) or "")[:1500],
            "is_short": (int(secs) <= 60) if secs else None}


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


# ---------- gemini ----------

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "person_speaks": {"type": "BOOLEAN"},
        "evidence": {"type": "STRING"},
        "quotes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "t": {"type": "STRING"}, "speaker": {"type": "STRING"}, "text": {"type": "STRING"},
            "topic": {"type": "STRING"}}, "required": ["t", "speaker", "text"]}},
        "stance": {"type": "OBJECT", "properties": {
            "us_equities": {"type": "STRING", "enum": ["bullish", "bearish", "neutral", "mixed", "not_stated"]},
            "summary": {"type": "STRING"},
            "targets": {"type": "ARRAY", "items": {"type": "STRING"}},
            "timeframe": {"type": "STRING"},
            "invalidation": {"type": "STRING"}}, "required": ["us_equities", "summary"]},
    },
    "required": ["person_speaks", "evidence", "quotes", "stance"],
}


def gemini_prompt(person: str, n: int) -> str:
    return (f"Tracked person: {person}.\n"
            "1. Does this person personally appear and speak in this video? Decide from what you hear and see, "
            "not from the title. Being mentioned by someone else does not count.\n"
            f"2. Give up to {n} short quotes (1-3 sentences each) where THIS person states a view on markets, "
            "the economy, rates, stocks, credit, volatility or positioning. Prefer quotes about US equity direction, "
            "price targets, dates or windows, and what would change their mind. Copy the words exactly as spoken. "
            "Do not paraphrase, do not fix grammar, do not merge separate sentences. Never attribute the host's or "
            "another guest's words to this person. t = start time as M:SS or H:MM:SS. speaker = who is speaking.\n"
            "3. stance: this person's own view on US equities in this video, based only on their own words. "
            "targets, timeframe and invalidation only if they actually said them, otherwise leave empty.\n"
            "If the person does not speak, return person_speaks=false, empty quotes and stance not_stated.")


class GeminiBusy(Exception):
    pass


class BudgetOut(Exception):
    """Ran out of this run's Gemini time budget. Not the video's fault: never counts as an error."""


def gemini_text_prompt(person: str, n: int) -> str:
    return (f"Tracked person: {person}.\n"
            "Below is an auto-generated caption transcript of a video. Lines start with [M:SS] or [H:MM:SS] markers. "
            "There are no speaker labels; '>>' usually marks a change of speaker. Captions may misspell names.\n"
            "1. From the conversation itself, decide whether this person personally speaks in the video. "
            "Being mentioned or introduced by someone else does not count by itself.\n"
            f"2. Give up to {n} short quotes (1-3 sentences each) that THIS person clearly says, stating a view on "
            "markets, the economy, rates, stocks, credit, volatility or positioning. Copy the caption words exactly. "
            "Use a passage only if the context makes it clear that this person is the one speaking "
            "(for example it answers a question addressed to them). Never use the host's or another guest's words. "
            "t = the nearest preceding [M:SS] marker. speaker = who is speaking.\n"
            "3. stance: this person's own view on US equities, based only on their own words.\n"
            "If the person does not clearly speak, return person_speaks=false, empty quotes and stance not_stated.")


def _gemini_call(http: Http, key: str, model: str, body: dict, sleep: Callable, clock: Callable, deadline: float):
    """One logical call with retries, never running past the deadline.
    5xx: up to 3 tries with backoff. 429: wait 65s and retry once."""
    status, text = None, ""
    tries_5xx, tries_429 = 0, 0
    while True:
        left = deadline - clock()
        if left < 60:
            raise BudgetOut()
        try:
            status, text, _ = http.post_json(f"{GEMINI_BASE}/{model}:generateContent", body,
                                             headers={"x-goog-api-key": key}, timeout=min(420, left - 10))
        except requests.Timeout:
            status, text = 504, "timeout"
        except Exception as e:
            raise RuntimeError(type(e).__name__)
        if status in (500, 502, 503, 504) and tries_5xx < 2:
            tries_5xx += 1
            wait = 20 * tries_5xx
        elif status == 429 and tries_429 < 1:
            tries_429 += 1
            wait = 65
        else:
            break
        if deadline - clock() - wait < 60:
            raise BudgetOut()
        sleep(wait)
    if status in (500, 502, 503, 504):
        raise GeminiBusy(str(status))
    if status == 429:
        raise QuotaExhausted()
    if status in (401, 403) and ("API key" in text or "PERMISSION_DENIED" in text):
        raise KeyInvalid()
    if status == 404:
        raise GeminiBusy("404 model")
    if status != 200:
        msg = ""
        try:
            msg = json.loads(text).get("error", {}).get("message", "")[:160]
        except Exception:
            pass
        raise RuntimeError(f"{status} {msg}".strip())
    j = json.loads(text)
    cand = (j.get("candidates") or [{}])[0]
    out = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if "text" in p)
    if not out:
        raise RuntimeError(f"empty ({cand.get('finishReason')})")
    return json.loads(out), j.get("usageMetadata") or {}


def _gen_config() -> dict:
    return {"temperature": 0, "responseMimeType": "application/json", "responseSchema": GEMINI_SCHEMA}


def fetch_gemini(http: Http, key: str, model: str, vid: str, person: str, cfg: dict, sleep: Callable,
                 clock: Callable = time.monotonic, deadline: float | None = None, start: int | None = None,
                 end: int | None = None):
    """Video mode. Optional clip [start, end) in seconds."""
    deadline = deadline if deadline is not None else clock() + 3600
    meta = {"fps": cfg.get("gemini_fps", 0.1)}
    if start is not None:
        meta["startOffset"] = f"{int(start)}s"
    if end is not None:
        meta["endOffset"] = f"{int(end)}s"
    part = {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={vid}"}, "videoMetadata": meta}
    gen = {**_gen_config(), "mediaResolution": "MEDIA_RESOLUTION_LOW"}
    body = {"contents": [{"parts": [part, {"text": gemini_prompt(person, cfg.get("gemini_max_quotes", 12))}]}],
            "generationConfig": gen}
    return _gemini_call(http, key, model, body, sleep, clock, deadline)


def fetch_gemini_text(http: Http, key: str, model: str, caption: str, person: str, cfg: dict, sleep: Callable,
                      clock: Callable = time.monotonic, deadline: float | None = None):
    """Caption-text mode: used when video mode failed but we hold captions. Uses no YouTube video hours."""
    deadline = deadline if deadline is not None else clock() + 3600
    body = {"contents": [{"parts": [{"text": gemini_text_prompt(person, cfg.get("gemini_max_quotes", 12))},
                                    {"text": "TRANSCRIPT:\n" + caption[:400000]}]}],
            "generationConfig": _gen_config()}
    return _gemini_call(http, key, model, body, sleep, clock, deadline)


def video_duration(http: Http, vid: str, caption: str | None) -> int | None:
    if caption:
        marks = re.findall(r"\[(\d+):(\d+)(?::(\d+))?\]", caption)
        if marks:
            a, b, c = marks[-1]
            return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
    try:
        status, text, _ = http.get(f"https://www.youtube.com/watch?v={vid}")
    except Exception:
        return None
    m = re.search(r'"lengthSeconds":"(\d+)"', text) if status == 200 else None
    return int(m.group(1)) if m else None


def chunk_plan(duration: int | None, cfg: dict) -> list[tuple[int | None, int | None]]:
    """Whole video, or 40-minute clips with 30s overlap for long videos (free-tier tokens per minute)."""
    limit = cfg.get("gemini_chunk_over_sec", 3000)
    size = cfg.get("gemini_chunk_sec", 2400)
    overlap = cfg.get("gemini_chunk_overlap_sec", 30)
    if not duration or duration <= limit:
        return [(None, None)]
    out, start = [], 0
    while start < duration:
        end = min(duration, start + size)
        out.append((start, end))
        if end >= duration:
            break
        start = end - overlap
    return out


def merge_chunks(results: list[tuple[dict, int | None]], max_total: int) -> dict:
    """Merge per-clip answers. Clip timestamps that come back relative to the clip start are moved to
    whole-video time, and quotes repeated in the overlap are dropped."""
    from rapidfuzz import fuzz
    speaks = any(r.get("person_speaks") for r, _ in results)
    quotes, seen = [], []
    stances, summaries = [], []
    for r, start in results:
        for q in r.get("quotes") or []:
            sec = ts_to_sec(q.get("t"))
            if start and sec is not None and sec < start:
                sec += start
                q = {**q, "t": f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"}
            nt = norm_text(str(q.get("text", "")))
            if any(fuzz.ratio(nt, x) >= 90 for x in seen):
                continue
            seen.append(nt)
            quotes.append(q)
        st = r.get("stance") or {}
        if r.get("person_speaks") and st.get("us_equities") not in (None, "not_stated"):
            stances.append(st["us_equities"])
            summaries.append(st.get("summary", ""))
    lean = stances[0] if stances and len(set(stances)) == 1 else ("mixed" if stances else "not_stated")
    return {"person_speaks": speaks,
            "evidence": " / ".join(str(r.get("evidence", ""))[:200] for r, _ in results),
            "quotes": quotes[:max_total],
            "stance": {"us_equities": lean, "summary": " | ".join(x for x in summaries if x)[:800]}}


def ts_to_sec(t) -> int | None:
    try:
        p = [int(x) for x in str(t).strip().split(":")]
    except ValueError:
        return None
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    if len(p) == 2:
        return p[0] * 60 + p[1]
    return None


def norm_text(s: str) -> str:
    s = s.lower().replace("\u2019", "'")
    s = re.sub(r"\[music\]|>>", " ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def caption_index(raw: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalized caption text plus (char position, seconds) marks from '[m:ss]' markers when present."""
    parts, marks, pos = [], [], 0
    found = list(re.finditer(r"\[(\d+):(\d+)(?::(\d+))?\]\s*([^\[]*)", raw))
    if not found:
        return norm_text(raw), []
    for m in found:
        a, b, c, seg = m.groups()
        secs = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
        seg = norm_text(seg)
        marks.append((pos, secs))
        parts.append(seg)
        pos += len(seg) + 1
    return " ".join(parts), marks


def speaker_ok(person: dict, speaker: str) -> bool:
    sp = (speaker or "").lower()
    return any(re.search(r"\b" + re.escape(n.lower()) + r"\b", sp) for n in person.get("speaker_names", person["names"]))


def verify_quotes(quotes: list[dict], caption_raw: str, threshold: float, min_chars: int) -> None:
    """Grade each quote on its own against the captions. Called only while captions are held; the grades
    are then stored on the quote and survive after the captions are deleted."""
    from rapidfuzz import fuzz
    idx = caption_index(caption_raw)
    for q in quotes:
        q["caption_match"], q["caption_sec"] = None, None
        q["grade"] = "1차-B"
        q.pop("caption_conflict", None)
        qt = norm_text(q.get("text", ""))
        if len(qt) < 15:
            continue
        al = fuzz.partial_ratio_alignment(qt, idx[0])
        q["caption_match"] = round(al.score, 1)
        sec = None
        for p, s_ in idx[1]:
            if p <= al.dest_start:
                sec = s_
            else:
                break
        q["caption_sec"] = sec
        if al.score >= threshold and len(qt) >= min_chars:
            q["grade"] = "1차-A"
        elif al.score >= threshold:
            q["grade_note"] = f"matched captions but shorter than {min_chars} chars, kept at 1차-B"
        elif al.score < 70:
            q["caption_conflict"] = True  # captions exist but this wording is not in them: treat with caution
        q["caption_checked"] = True


def clean_quotes(raw: list, vid: str, n: int) -> list[dict]:
    out = []
    for q in (raw or [])[:n]:
        text = str(q.get("text", "")).strip()
        if not text:
            continue
        sec = ts_to_sec(q.get("t"))
        out.append({"t": q.get("t"), "t_sec": sec, "speaker": str(q.get("speaker", ""))[:80],
                    "topic": str(q.get("topic", ""))[:80], "text": text[:600],
                    "url": f"https://www.youtube.com/watch?v={vid}" + (f"&t={sec}s" if sec is not None else "")})
    return out


GEMINI_DEFAULTS = {"gemini_status": "pending", "gemini_model": None, "gemini_at": None, "gemini_mode": None,
                   "person_speaks": None, "gemini_evidence": None, "stance": None, "quotes": [],
                   "gemini_error_count": 0, "gemini_last_error": None, "evidence_ready_at": None,
                   "evidence_grade": "제목·설명만 확인", "appearance_source": None, "transcript_retained": False}


# ---------- discovery ----------

def new_record(e: dict, first_seen_at: str) -> dict:
    return {
        "video_id": e["video_id"],
        "url": f"https://www.youtube.com/watch?v={e['video_id']}",
        "channel": e["channel"],
        "channel_id": e["channel_id"],
        "channel_mode": e["channel_mode"],
        "person": e["person"],
        "tier": e["tier"],
        "match": e["match"],
        "title": e["title"],
        "description": e["description"][:1500],
        "published_at": e["published_at"],
        "first_seen_at": first_seen_at,
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
        **{k: (list(d) if isinstance(d, list) else d) for k, d in GEMINI_DEFAULTS.items()},
    }


def discover(http: Http, cfg: dict, state: dict, people: list, by_person: dict, window: timedelta, now: datetime,
             prev_gen: str | None, errors: dict) -> tuple[list[dict], list[dict]]:
    """Read every allowlisted channel's RSS and return (channel status, matched entries within the window)."""
    channel_out, found = [], []
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
            found.append({**e, "channel": label, "channel_id": cid, "channel_mode": ch["mode"],
                          "person": p["person"], "tier": p["tier"], "match": field})
        info["matched"] = matched
        channel_out.append(info)
    return channel_out, found


def run_discover(http: Http | None = None, now: datetime | None = None) -> dict:
    """Hourly, free: RSS only. Parks newly matched videos in discovered.json so high-volume channels
    (RSS shows only the latest 15) are not missed between the twice-daily full runs.
    Never touches youtube_feed.json, captions or Gemini."""
    http = http or Http()
    now = now or now_utc()
    cfg = load_json(CONFIG_PATH, {})
    people = cfg["people"]
    by_person = {p["person"]: p for p in people}
    window = timedelta(days=cfg.get("window_days", 7))
    max_age = timedelta(hours=cfg.get("transcript_max_age_hours", 48))
    state = load_json(STATE_PATH, {"channel_ids": {}, "yta": {}, "supadata": {"used_total": 0}})
    feed = load_json(FEED_PATH, {})
    known = {v["video_id"] for v in feed.get("videos", [])}
    queue = load_json(QUEUE_PATH, {"videos": []})
    queued = {q["video_id"] for q in queue["videos"]}
    last_check = queue.get("last_checked_at") or feed.get("generated_at")
    errors: dict[str, str] = {}
    channel_out, found = discover(http, cfg, state, people, by_person, window, now, last_check, errors)
    added = []
    for e in found:
        if e["video_id"] in known or e["video_id"] in queued:
            continue
        # backlog guard: anything already older than the transcript age limit is left to the full run's rules
        if now - parse_ts(e["published_at"]) > max_age:
            continue
        added.append({**e, "description": e["description"][:1500], "first_seen_at": iso_kst(now)})
    gaps = sorted(c["label"] for c in channel_out if c.get("gap_possible"))
    changed = bool(added) or errors != queue.get("errors", {}) or gaps != queue.get("gap_possible", [])
    queue["videos"] = queue["videos"] + added
    queue.update(last_checked_at=iso_kst(now), errors=errors, gap_possible=gaps)
    if changed or not QUEUE_PATH.exists():
        write_json(QUEUE_PATH, queue)
    if state.get("channel_ids") != load_json(STATE_PATH, {}).get("channel_ids"):
        write_json(STATE_PATH, state)
    return {"added": [(a["person"], a["channel"], a["title"]) for a in added], "queued_total": len(queue["videos"]),
            "errors": errors, "gap_possible": gaps, "file_written": changed}


# ---------- main ----------

def run(yta_key: str | None, supadata_key: str | None, http: Http | None = None, now: datetime | None = None,
        sleep: Callable = time.sleep, gemini_key: str | None = None, clock: Callable = time.monotonic) -> dict:
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
    for v in videos.values():  # migrate older records
        for k, d in GEMINI_DEFAULTS.items():
            v.setdefault(k, list(d) if isinstance(d, list) else d)
        if v["gemini_status"] == "pending" and v.get("transcript_status") in ("skipped_old", "skipped_short"):
            v["gemini_status"] = "skipped"
    migrations = set(state.get("migrations", []))
    if "evidence_clock_v2" not in migrations:
        # Before PR #8 the evidence clock could start on captions alone. Under the current rule it starts only
        # when speaker-verified quotes exist, so clear clocks that have no such quotes yet; they will be set
        # (once) when the quotes arrive. Also forget error counts caused only by our own Gemini quota.
        for v in videos.values():
            person = by_person.get(v.get("person"))
            good = [q for q in v.get("quotes") or []
                    if q.get("speaker_verified", person is not None and speaker_ok(person, q.get("speaker", "")))]
            if v.get("evidence_ready_at") and not good:
                v["evidence_ready_at"] = None
            if v.get("gemini_status") == "error" and str(v.get("gemini_last_error") or "").startswith("quota"):
                v["gemini_error_count"] = 0
        migrations.add("evidence_clock_v2")
    state["migrations"] = sorted(migrations)
    prev_gen = old.get("generated_at")
    errors: dict[str, str] = {}
    fetched_at = iso_kst(now)

    # discovery: hourly discovery-only runs park new videos in discovered.json; merge them first
    queue = load_json(QUEUE_PATH, {"videos": []})
    merged = 0
    for q in queue.get("videos", []):
        if q["video_id"] not in videos:
            videos[q["video_id"]] = new_record(q, q["first_seen_at"])
            merged += 1
    discovery_info = {"last_discovery_at": queue.get("last_checked_at"), "merged_from_discovery": merged,
                      "discovery_errors": queue.get("errors", {})}
    channel_out, found = discover(http, cfg, state, people, by_person, window, now, prev_gen, errors)
    for e in found:
        v = videos.get(e["video_id"])
        if v:
            v.update(title=e["title"], description=e["description"][:1500])
        else:
            videos[e["video_id"]] = new_record(e, fetched_at)

    # extra videos found outside channel RSS (web discovery, or listed by hand in channels.json "extra_videos").
    # Each is checked on its own watch page: it must be on an allowlisted channel and inside the window.
    # Accepted ones become normal records (Gemini + captions + per-quote grades); published_at stays the real date.
    allowed = {c["channel_id"]: c["label"] for c in channel_out if c.get("channel_id")}
    label_to_id = {lbl.strip().lower(): cid for cid, lbl in allowed.items()}
    modes = {c.get("channel_id") or state["channel_ids"].get(c["label"]): c["mode"] for c in cfg["channels"]}
    seen_extra = set(state.get("extra_checked", []))
    extra_in = list(cfg.get("extra_videos", [])) + list(queue.get("extra_videos", []))
    extra_stats = {"accepted": 0, "rejected": 0}
    for ex in extra_in:
        vid, person = ex.get("video_id"), ex.get("person")
        if not vid or vid in videos or vid in seen_extra or person not in by_person:
            continue
        meta = video_meta(http, vid, supadata_key, state)
        if not meta.get("channel_name"):
            extra_stats.setdefault("retry_next_run", []).append(vid)  # transient: try again next run
            continue
        cid = label_to_id.get(meta["channel_name"].strip().lower()) or (meta.get("channel_id") if meta.get("channel_id") in allowed else None)
        pub = meta.get("published_at") or ex.get("published_at")
        reason = None
        if not cid:
            reason = f"channel {meta.get('channel_name')} not allowlisted"
        elif not pub:
            extra_stats.setdefault("retry_next_run", []).append(vid)  # no reliable date yet
            continue
        else:
            pub = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            if now - parse_ts(pub) > window:
                reason = "older than window"
        seen_extra.add(vid)
        if reason:
            extra_stats["rejected"] += 1
            extra_stats.setdefault("reasons", {})[vid] = reason
            continue
        p_ = by_person[person]
        videos[vid] = new_record({"video_id": vid, "channel": allowed[cid], "channel_id": cid,
                                  "channel_mode": modes.get(cid, "filter"), "person": person, "tier": p_["tier"],
                                  "match": ex.get("source", "extra"), "title": meta.get("title", ""),
                                  "description": meta.get("description", ""), "published_at": pub}, fetched_at)
        videos[vid]["is_short"] = meta.get("is_short")
        extra_stats["accepted"] += 1
    state["extra_checked"] = sorted(seen_extra)[-500:]
    discovery_info["extra_videos"] = extra_stats

    # one-time manual backfill: exactly the video_ids listed in channels.json "backfill_once", each at most once ever.
    # published_at is untouched, so freshness still counts from the day the person spoke.
    done = set(state.get("backfill_done", []))
    for vid in cfg.get("backfill_once", []):
        v = videos.get(vid)
        if v and vid not in done and v["transcript_status"] == "skipped_old":
            v.update(transcript_status="pending_quota", error_count=0, last_error=None,
                     gemini_status="pending", gemini_error_count=0, gemini_last_error=None,
                     backfilled_at=fetched_at)
            done.add(vid)
    state["backfill_done"] = sorted(done)

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

    # one-time migration: captions used to be committed to the public repo. Move them to the private cache.
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    (TRANSCRIPT_DIR / ".keep").touch()
    if LEGACY_TRANSCRIPT_DIR.exists():
        for f in LEGACY_TRANSCRIPT_DIR.glob("*.txt"):
            dst = TRANSCRIPT_DIR / f.name
            if not dst.exists():
                dst.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            f.unlink()
        try:
            LEGACY_TRANSCRIPT_DIR.rmdir()
        except OSError:
            pass

    def caption_of(v: dict) -> str | None:
        f = TRANSCRIPT_DIR / f"{v['video_id']}.txt"
        return f.read_text(encoding="utf-8") if v["transcript_status"] == "ready" and f.exists() else None

    def needs_gemini_first(v: dict) -> bool:
        # filter channel matched only through the description (often an old-episode list): spend no caption
        # quota until Gemini has confirmed the person actually speaks
        return bool(gemini_key) and v["channel_mode"] == "filter" and v.get("match") == "description" \
            and v.get("gemini_status") != "ready"

    stats = {"attempted": 0, "ready": 0, "unavailable": 0, "errors": 0, "skipped_short": 0, "expired": 0,
             "left_pending": 0, "waiting_for_gemini": 0}

    def caption_pass(first_pass: bool) -> None:
        nonlocal yta_ok, sup_ok
        todo = [v for v in videos.values() if v["transcript_status"] in ("pending_quota", "error")]
        todo.sort(key=lambda v: (0 if v.get("first_seen_at") != fetched_at else 1, v["tier"],
                                 -parse_ts(v["published_at"]).timestamp()))
        for v in todo:
            age = now - parse_ts(v["published_at"])
            if first_pass and age > max_age and v["transcript_status"] == "pending_quota" and v["error_count"] == 0 \
                    and v.get("first_seen_at") == fetched_at and v.get("match") not in EXTRA_MATCHES:
                v["transcript_status"] = "skipped_old"
                v["gemini_status"] = "skipped"
                v["last_error"] = "older than transcript_max_age_hours when first discovered (backfill limit)"
                stats["expired"] += 1
                continue
            if age > window:
                v["transcript_status"] = "unavailable"
                v["last_error"] = "expired (older than window)"
                stats["expired"] += 1
                continue
            if cfg.get("skip_shorts", True) and v.get("is_short") is None:
                v["is_short"] = is_short(http, v["video_id"])
            if v.get("is_short"):
                v["transcript_status"] = "skipped_short"
                v["gemini_status"] = "skipped"
                v["last_error"] = "short (skipped)"
                stats["skipped_short"] += 1
                continue
            if v.get("gemini_status") == "not_speaking":
                v["transcript_status"] = "not_needed"
                v["last_error"] = "Gemini: person does not speak"
                continue
            if needs_gemini_first(v):
                if first_pass:
                    stats["waiting_for_gemini"] += 1
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
                cnt, excerpt = appearance(by_person[v["person"]], text)
                v.update(transcript_status="ready", transcript_provider=provider, transcript_language=lang,
                         transcript_chars=len(text), transcript_url=None, name_mentions=cnt,
                         appearance_excerpt=excerpt, last_error=None)
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

    caption_pass(True)

    # gemini: direct video analysis, T1 -> T2 -> T3, newest first, within a hard time budget
    gem_day = state.get("gemini") or {}
    if gem_day.get("date") != today:
        gem_day = {"date": today, "calls": 0, "est_video_hours": 0.0}
    gem_models = list(cfg.get("gemini_models", ["models/gemini-3.5-flash", "models/gemini-flash-latest"]))
    gem_ok = bool(gemini_key)
    gem_note = "ok" if gem_ok else "no key"
    gem_budget = cfg.get("gemini_time_budget_sec", 900)
    gem_max_age = timedelta(hours=cfg.get("gemini_max_age_hours", 48))
    gem_hours_cap = cfg.get("gemini_daily_video_hours", 7.5)
    gem_tok_per_sec = cfg.get("gemini_tokens_per_video_sec", 38)
    max_q = cfg.get("gemini_max_quotes", 12)
    gstats = {"attempted": 0, "ready": 0, "not_speaking": 0, "busy": 0, "errors": 0, "left_pending": 0,
              "budget_out": 0, "chunked": 0, "text_mode": 0, "quota_wait": 0}
    deadline = clock() + gem_budget
    gtodo = [v for v in videos.values() if v["gemini_status"] in ("pending", "error")
             and v.get("transcript_status") not in ("skipped_old", "skipped_short")]
    # tier first; inside a tier, videos already waiting from earlier runs (captions held, quota/budget misses)
    # go before newly found ones, so a freshness-critical carry-over is not pushed back by new uploads
    gtodo.sort(key=lambda v: (v["tier"], 0 if v.get("first_seen_at") != fetched_at else 1,
                              -parse_ts(v["published_at"]).timestamp()))

    exhausted: set[str] = set()  # free-tier quotas are per model: a 429 on one model does not stop the others

    def try_models(fn):
        """Run fn(model) over the model list. Returns (parsed, usage, model, last_error)."""
        nonlocal gem_ok, gem_note
        last = None
        for model in gem_models:
            if model in exhausted:
                continue
            gstats["attempted"] += 1
            gem_day["calls"] += 1
            try:
                parsed, usage = fn(model)
                return parsed, usage, model, None
            except GeminiBusy as e:
                last = f"busy {e} ({model.split('/')[-1]})"
                continue
            except QuotaExhausted:
                exhausted.add(model)
                last = last or f"quota (429) {model.split('/')[-1]}"
                if all(m in exhausted for m in gem_models):
                    gem_ok, gem_note = False, "quota or rate limit (429) on every model"
                    return None, {}, None, "quota (429) on every model"
                continue
            except KeyInvalid:
                gem_ok, gem_note = False, "key invalid"
                return None, {}, None, "key invalid"
            except BudgetOut:
                raise
            except Exception as e:
                return None, {}, None, f"error {str(e)[:160]}"
        return None, {}, None, last

    for v in gtodo:
        age = now - parse_ts(v["published_at"])
        if age > window:
            v["gemini_status"], v["gemini_last_error"] = "unavailable", "expired (older than window)"
            continue
        if v["gemini_status"] == "pending" and v["gemini_error_count"] == 0 and age > gem_max_age \
                and v.get("first_seen_at") == fetched_at and v.get("match") not in EXTRA_MATCHES:
            v["gemini_status"], v["gemini_last_error"] = "skipped", "older than gemini_max_age_hours when first discovered"
            continue
        if not gem_ok or deadline - clock() < 60 or gem_day["est_video_hours"] >= gem_hours_cap:
            gstats["left_pending" if gem_ok and deadline - clock() >= 60 else "budget_out"] += 1
            continue
        caption = caption_of(v)
        parsed, usage, model, last_err, mode = None, {}, None, None, None
        try:
            plan = chunk_plan(video_duration(http, v["video_id"], caption), cfg)
            if len(plan) > 1:
                gstats["chunked"] += 1
                parts, ok = [], True
                for i, (st, en) in enumerate(plan):
                    if i:
                        sleep(min(65, max(0, deadline - clock() - 120)))  # tokens-per-minute spacing
                    r, u, m, err = try_models(lambda mdl: fetch_gemini(http, gemini_key, mdl, v["video_id"], v["person"],
                                                                       cfg, sleep, clock, deadline, st, en))
                    if r is None:
                        ok, last_err = False, err
                        break
                    parts.append((r, st))
                    usage = {"promptTokenCount": (usage.get("promptTokenCount") or 0) + (u.get("promptTokenCount") or 0)}
                    model = m
                if ok:
                    parsed, mode = merge_chunks(parts, max_q * 2), f"video_chunks_{len(plan)}"
            else:
                parsed, usage, model, last_err = try_models(
                    lambda mdl: fetch_gemini(http, gemini_key, mdl, v["video_id"], v["person"], cfg, sleep, clock, deadline))
                mode = "video" if parsed is not None else None
            if parsed is None and caption and gem_ok and len(exhausted) < len(gem_models):
                # video path failed: read our own captions instead (no YouTube video hours, small token count)
                parsed, usage, model, err2 = try_models(
                    lambda mdl: fetch_gemini_text(http, gemini_key, mdl, caption, v["person"], cfg, sleep, clock, deadline))
                if parsed is not None:
                    mode, usage = "caption_text", {}
                    gstats["text_mode"] += 1
                else:
                    last_err = f"{last_err}; text: {err2}"
        except BudgetOut:
            gstats["budget_out"] += 1
            continue  # leave status as it was: not an error
        if parsed is None and last_err and last_err.startswith("quota"):
            v["gemini_last_error"] = last_err  # our quota, not the video's fault: retry next run, no error count
            gstats["quota_wait"] += 1
            continue
        if parsed is None:
            v["gemini_error_count"] += 1
            v["gemini_last_error"] = last_err
            v["gemini_status"] = "unavailable" if v["gemini_error_count"] >= cfg.get("max_error_retries", 3) else "error"
            gstats["busy" if last_err and last_err.startswith("busy") else "errors"] += 1
            continue
        tokens = usage.get("promptTokenCount") or 0
        gem_day["est_video_hours"] = round(gem_day["est_video_hours"] + tokens / gem_tok_per_sec / 3600, 3)
        speaks = bool(parsed.get("person_speaks"))
        person = by_person[v["person"]]
        qs = clean_quotes(parsed.get("quotes"), v["video_id"], max_q * 2) if speaks else []
        for q in qs:
            q["speaker_verified"] = speaker_ok(person, q["speaker"])
        v.update(gemini_at=fetched_at, gemini_model=model, gemini_mode=mode, person_speaks=speaks,
                 gemini_evidence=str(parsed.get("evidence", ""))[:400],
                 stance=parsed.get("stance") if speaks else None, quotes=qs,
                 gemini_status="ready" if speaks else "not_speaking", gemini_last_error=None,
                 gemini_prompt_tokens=tokens)
        gstats["ready" if speaks else "not_speaking"] += 1
        sleep(max(3, min(60, tokens / 250000 * 60, max(0, deadline - clock() - 60))))

    caption_pass(False)  # description-only matches that Gemini just confirmed get their captions now

    # per-video: verification while captions are held, grades, evidence clock, caption retention
    threshold = cfg.get("quote_verify_threshold", 90)
    min_a = cfg.get("quote_min_chars_for_a", 40)
    for v in videos.values():
        caption = caption_of(v)
        if caption and v["person"] in by_person:
            cnt, excerpt = appearance(by_person[v["person"]], caption)
            v.update(name_mentions=cnt, appearance_excerpt=excerpt)
        if v["gemini_status"] in ("ready", "not_speaking"):
            v["appearance_confirmed"], v["appearance_source"] = bool(v["person_speaks"]), "gemini"
        elif v["channel_mode"] == "dedicated":
            v["appearance_confirmed"], v["appearance_source"] = True, "dedicated_channel"
        else:
            v["appearance_confirmed"], v["appearance_source"] = None, None  # unconfirmed until Gemini answers
        for q in v["quotes"]:
            q.setdefault("speaker_verified", speaker_ok(by_person[v["person"]], q.get("speaker", "")))
        if v["quotes"] and caption:
            verify_quotes(v["quotes"], caption, threshold, min_a)
        good = [q for q in v["quotes"] if q.get("speaker_verified")]
        a = sum(q.get("grade") == "1차-A" for q in good)
        v["quotes_grade_count"] = {"1차-A": a, "1차-B": len(good) - a,
                                   "excluded_speaker_mismatch": len(v["quotes"]) - len(good)}
        if v["appearance_confirmed"] is False:
            v["evidence_grade"] = "본인 발언 없음"
        elif a:
            v["evidence_grade"] = "1차-A"
        elif good:
            v["evidence_grade"] = "1차-B"
        elif caption:
            v["evidence_grade"] = "자막 확보, 인용 대기"
        else:
            v["evidence_grade"] = "제목·설명만 확인"
        if not v.get("evidence_ready_at") and v["appearance_confirmed"] and good:
            v["evidence_ready_at"] = fetched_at
        # captions are kept (privately) only while they can still be used: verification or a text-mode retry
        f = TRANSCRIPT_DIR / f"{v['video_id']}.txt"
        done = v["gemini_status"] in ("ready", "not_speaking", "unavailable", "skipped") and \
            (not v["quotes"] or all(q.get("caption_checked") for q in v["quotes"]))
        if f.exists() and done:
            f.unlink()
        v["transcript_retained"] = f.exists()
        v["transcript_url"] = None

    # prune
    cutoff = now - window
    keep = {k: v for k, v in videos.items() if v.get("published_at") and parse_ts(v["published_at"]) >= cutoff}
    for k in set(videos) - set(keep):
        f = TRANSCRIPT_DIR / f"{k}.txt"
        if f.exists():
            f.unlink()
    for f in TRANSCRIPT_DIR.glob("*.txt") if TRANSCRIPT_DIR.exists() else []:
        if f.stem not in keep:
            f.unlink()
    vids = sorted(keep.values(), key=lambda v: v["published_at"], reverse=True)

    for name, note in provider_notes.items():
        if note not in ("ok", "no key", "quota exhausted"):
            errors[name] = note
    if not yta_key and not supadata_key:
        errors["_transcripts"] = "자막 키 없음 (YT_TRANSCRIPT_KEY, SUPADATA_KEY)"
    if gem_note == "key invalid":
        errors["gemini"] = gem_note

    state["yta"], state["supadata"], state["gemini"] = yta, sup, gem_day
    feed = {
        "schema": "guru-yt/v1",
        "generated_at": fetched_at,
        "data_ok": not errors,
        "source": "Discovery via allowlisted channel RSS. quotes[] are extracted by Gemini from the video itself "
                  "(gemini_mode video or video_chunks_N) or, if that failed, from our captions (caption_text). "
                  "Each quote carries its own grade: 1차-A = matched the captions >= quote_verify_threshold and is at "
                  "least quote_min_chars_for_a long; otherwise 1차-B. Quotes with speaker_verified=false are kept "
                  "for audit but excluded from grade counts and must not be used as evidence. Captions are never "
                  "published: they are held privately only until verification is done. "
                  "stance is Gemini's reading of the person's own words, not a quote.",
        "new_statement_clock": "evidence_ready_at",
        "generated_at_note": "generated_at is the time of the last full run (captions + Gemini). "
                             "Discovery-only runs never change it; see discovery.last_discovery_at.",
        "discovery": discovery_info,
        "quote_verify_threshold": threshold,
        "quote_min_chars_for_a": min_a,
        "errors": errors,
        "channels": channel_out,
        "quota": {"yta_used_today": yta["used"], "yta_server_remaining": yta.get("server_remaining"),
                  "yta_daily_cap": yta_cap, "yta_reserve_for_manual": reserve,
                  "supadata_used_total": sup["used_total"], "supadata_total_cap": sup_cap,
                  "providers": provider_notes,
                  "gemini_calls_today": gem_day["calls"], "gemini_est_video_hours_today": gem_day["est_video_hours"],
                  "gemini_daily_video_hours_cap": gem_hours_cap, "gemini": gem_note},
        "stats": stats,
        "gemini_stats": gstats,
        "videos": vids,
    }
    write_json(STATE_PATH, state)
    write_json(FEED_PATH, feed)
    write_json(QUEUE_PATH, {"videos": [], "last_checked_at": fetched_at, "errors": {}, "gap_possible": []})
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
    if "--discover" in sys.argv:
        print(json.dumps(run_discover(), ensure_ascii=False, indent=2))
        return 0
    feed = run(os.environ.get("YT_TRANSCRIPT_KEY") or None, os.environ.get("SUPADATA_KEY") or None,
               gemini_key=os.environ.get("GEMINI_API_KEY") or None)
    summary = {k: feed[k] for k in ("generated_at", "data_ok", "errors", "quota", "stats", "gemini_stats")}
    summary["channels"] = [{k: c.get(k) for k in ("label", "rss_ok", "matched", "error")} for c in feed["channels"]]
    summary["videos"] = [{k: v.get(k) for k in ("person", "channel", "title", "transcript_status", "gemini_status",
                                                 "appearance_confirmed", "evidence_grade", "quotes_grade_count")}
                         for v in feed["videos"][:20]]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
