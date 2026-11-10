"""Transcribe videos attached to tracked X posts with Gemini (free tier).

Many posts are video-only (e.g. Cem Karsan posts show clips with just a link as text). Without this, the system
sees only the poster frame. For each video: download the lowest-bitrate public mp4 from video.twimg.com, extract
mono audio and four still frames with ffmpeg, send them inline to Gemini, and keep verbatim quotes with timestamps.

Grading: quotes are machine transcription without a caption cross-check, so at most 1차-B, and only when the
tracked person is identified as the speaker from visible or spoken evidence (on-screen name, self-introduction,
introduced by the host, addressed by name). A voice alone never identifies anyone. Media files are never stored.
The API key is sent only as a header.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from typing import Callable

import requests

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODELS = ["models/gemini-3.5-flash", "models/gemini-flash-latest", "models/gemini-3.5-flash-lite"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; guru-video/1.0)"}
NOTE = ("Gemini transcription of the video attached to the post. Quotes are machine-transcribed without a caption "
        "check: evidence at most 1차-B and only for quotes with speaker_verified=true. key_points and direction_hint "
        "are Gemini's reading, never evidence.")
ID_BASES = {"on_screen_name", "self_introduction", "introduced_by_host", "addressed_by_name"}
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "speakers": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"},
            "basis": {"type": "STRING", "enum": sorted(ID_BASES | {"voice_only_unidentified"})},
        }, "required": ["name", "basis"]}},
        "tracked_person_speaks": {"type": "BOOLEAN"},
        "show": {"type": "STRING"},
        "recorded_hint": {"type": "STRING"},
        "quotes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "text": {"type": "STRING"}, "t": {"type": "STRING"}, "speaker": {"type": "STRING"},
        }, "required": ["text", "t", "speaker"]}},
        "key_points": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "point": {"type": "STRING"}, "condition": {"type": "STRING"},
            "conviction": {"type": "STRING", "enum": ["stated", "hypothetical", "not_expected", "risk_scenario"]},
        }, "required": ["point", "condition", "conviction"]}},
        "direction_hint": {"type": "STRING", "enum": ["bullish", "bearish", "neutral", "mixed", "none"]},
    },
    "required": ["speakers", "tracked_person_speaks", "show", "recorded_hint", "quotes", "key_points", "direction_hint"],
}
PROMPT = (
    "Attached: the audio track of a video posted on X by {person} (@{handle}) (absent if the video has no sound), "
    "then up to six still frames from it.\n"
    "Post text: \"{text}\"\n"
    "Return JSON:\n"
    "- speakers: everyone who speaks, with name (\"unknown\" if not identifiable) and basis: on_screen_name "
    "(name shown in a frame), self_introduction, introduced_by_host, addressed_by_name, or voice_only_unidentified. "
    "Never identify anyone from voice alone, and never assume the poster is the speaker.\n"
    "- tracked_person_speaks: true only if {person} is one of the speakers with a basis other than "
    "voice_only_unidentified.\n"
    "- show: the program, channel or event if shown or said, else \"unknown\". recorded_hint: a recording date if "
    "shown or said, else \"\".\n"
    "- quotes: up to 12 verbatim English sentences spoken by {person} about markets, the economy, positioning or "
    "timing, exactly as said (40 to 300 characters each), with t = start time as m:ss and speaker = {person}. "
    "Empty if {person} does not speak or cannot be identified. Never include words of hosts or other guests.\n"
    "- key_points: up to 8 points {person} makes, one English sentence each, with the stated condition (or \"\") "
    "and conviction: stated, hypothetical, not_expected or risk_scenario.\n"
    "- direction_hint: the US-equity direction {person} states for roughly the next 1 to 3 months: bullish, bearish, "
    "neutral, mixed, or none if not stated."
)


def videos_of(p: dict) -> list[dict]:
    own = [dict(v, of="post") for v in p.get("videos") or []]
    quoted = [dict(v, of="quoted_post") for v in (p.get("quoted_post") or {}).get("videos") or []]
    return (own + quoted)[:1]


def _ffmpeg(args: list[str], timeout: int = 180) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=timeout,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract(mp4: bytes, duration: float) -> tuple[bytes | None, list[bytes]]:
    """Mono audio (None when the video has no audio track) and six frames; early frames catch name captions."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "v.mp4")
        open(src, "wb").write(mp4)
        aud = os.path.join(d, "a.mp3")
        try:
            _ffmpeg(["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "24k", aud])
            audio = open(aud, "rb").read() or None
        except Exception:
            audio = None
        if not duration:
            try:
                out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
                                     capture_output=True, text=True, timeout=30).stdout.strip()
                duration = float(out or 0)
            except Exception:
                duration = 0
        frames = []
        for i, frac in enumerate((0.02, 0.1, 0.25, 0.45, 0.65, 0.85)):
            f = os.path.join(d, f"f{i}.jpg")
            try:
                _ffmpeg(["-ss", f"{max(0.0, duration * frac):.1f}", "-i", src, "-frames:v", "1", "-vf", "scale=640:-2", f], 60)
                frames.append(open(f, "rb").read())
            except Exception:
                pass
        if audio is None and not frames:
            raise RuntimeError("no audio and no frames")
        return audio, frames


def _call(key: str, models: list[str], parts: list[dict], post: Callable, sleep: Callable) -> tuple[dict, str]:
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": SCHEMA}}
    last = "no model"
    for model in models:
        for attempt in range(2):
            r = post(f"{GEMINI_BASE}/{model}:generateContent", body, key)
            if r.status_code == 200:
                j = r.json()
                txt = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])
                return json.loads(txt), model
            if r.status_code == 429:
                last = "quota"
                break
            if r.status_code in (500, 502, 503, 504) and attempt == 0:
                sleep(10)
                continue
            last = f"http {r.status_code}"
            break
    raise RuntimeError(last)


def _same_person(name: str, person: str) -> bool:
    n, last = (name or "").lower(), person.lower().split()[-1]
    return bool(n) and n != "unknown" and (person.lower() in n or last in n.split())


def transcribe_posts(posts: list[dict], state: dict, now: datetime, key: str | None, cfg: dict,
                     get: Callable | None = None, post: Callable | None = None, extractor: Callable = extract,
                     sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> dict:
    get = get or (lambda u: requests.get(u, headers=UA, timeout=120))
    post = post or (lambda url, body, k: requests.post(url, json=body, timeout=300,
                                                       headers={"x-goog-api-key": k, "Content-Type": "application/json"}))
    models = cfg.get("models", DEFAULT_MODELS)
    day = now.strftime("%Y-%m-%d")
    usage = state.get("video_audio") or {}
    if usage.get("date") != day:
        usage = {"date": day, "calls": 0}
    daily_cap, run_cap = cfg.get("daily_cap", 20), cfg.get("run_cap", 6)
    max_sec, max_bytes = cfg.get("max_minutes", 40) * 60, cfg.get("max_mb", 150) * 1_000_000
    max_age = timedelta(hours=cfg.get("max_age_hours", 168))
    budget, t0 = cfg.get("time_budget_sec", 420), clock()
    stats = {"status": "ok" if key else "no key", "transcribed": 0, "verified_quotes": 0, "pending": 0, "skipped": 0,
             "errors": 0, "calls_today": usage["calls"], "daily_cap": daily_cap, "note": NOTE}

    def ts(p: dict) -> datetime:
        return datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))

    todo = [p for p in posts if videos_of(p) and (p.get("video_transcript") or {}).get("status") not in ("ready", "too_long")
            and (p.get("video_transcript") or {}).get("tries", 0) < 3 and now - ts(p) <= max_age]
    todo.sort(key=lambda p: (p.get("tier", 9), -ts(p).timestamp()))
    done = 0
    for p in todo:
        if not key or usage["calls"] >= daily_cap or done >= run_cap or clock() - t0 > budget:
            stats["pending"] += 1
            continue
        v = videos_of(p)[0]
        prev = p.get("video_transcript") or {}
        base = {"of": v["of"], "duration_sec": round(v.get("duration_ms", 0) / 1000), "note": NOTE}
        if v.get("duration_ms", 0) / 1000 > max_sec:
            p["video_transcript"] = {**base, "status": "too_long"}
            stats["skipped"] += 1
            continue
        try:
            r = get(v["mp4"])
            if r.status_code != 200 or not r.content or len(r.content) > max_bytes:
                raise RuntimeError(f"download {r.status_code}")
            audio, frames = extractor(r.content, v.get("duration_ms", 0) / 1000)
        except Exception as e:
            p["video_transcript"] = {**base, "status": "media_unavailable", "tries": prev.get("tries", 0) + 1,
                                     "error": str(e)[:80]}
            stats["errors"] += 1
            continue
        parts = [{"text": PROMPT.format(person=p.get("person"), handle=p.get("handle"), text=(p.get("text") or "")[:600])}]
        if audio:
            parts.append({"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(audio).decode()}})
        parts += [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(f).decode()}} for f in frames]
        try:
            usage["calls"] += 1
            done += 1
            res, model = _call(key, models, parts, post, sleep)
        except Exception as e:
            p["video_transcript"] = {**base, "status": "error", "tries": prev.get("tries", 0) + 1, "error": str(e)[:80]}
            stats["errors"] += 1
            if str(e) == "quota":
                key = None
                stats["status"] = "quota"
            continue
        person = p.get("person") or ""
        speakers = res.get("speakers") or []
        identified = bool(res.get("tracked_person_speaks")) and any(
            _same_person(s.get("name"), person) and s.get("basis") in ID_BASES for s in speakers)
        quotes = []
        for q in (res.get("quotes") or [])[:12]:
            text = (q.get("text") or "").strip()
            if len(text) < 20:
                continue
            quotes.append({"text": text[:300], "t": (q.get("t") or "")[:8],
                           "speaker_verified": identified and _same_person(q.get("speaker"), person)})
        nver = sum(q["speaker_verified"] for q in quotes)
        p["video_transcript"] = {
            **base, "status": "ready", "model": model, "at": now.isoformat(timespec="seconds"),
            "speakers": [{"name": (s.get("name") or "")[:60], "basis": s.get("basis")} for s in speakers][:6],
            "tracked_person_identified": identified,
            "show": (res.get("show") or "unknown")[:80], "recorded_hint": (res.get("recorded_hint") or "")[:40],
            "has_audio": bool(audio),
            "evidence_grade": "1차-B" if nver else ("본인 발언 없음" if not identified else "인용 없음"),
            "quotes": quotes,
            "key_points": [{"point": (k.get("point") or "")[:300], "condition": (k.get("condition") or "")[:200],
                            "conviction": k.get("conviction")} for k in (res.get("key_points") or [])[:8]],
            "direction_hint": res.get("direction_hint") or "none",
        }
        stats["transcribed"] += 1
        stats["verified_quotes"] += nver
    usage["calls"] = usage["calls"]
    state["video_audio"] = usage
    stats["calls_today"] = usage["calls"]
    return stats
