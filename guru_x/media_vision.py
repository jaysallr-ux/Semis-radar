"""Describe images attached to tracked X posts with a light Gemini model.

Context only. The description says what an attached chart/screenshot shows and transcribes visible text;
it is never the tracked person's own words and must never set stance, freshness, convergence or alerts.
Images are fetched from pbs.twimg.com, sent inline, and never stored. The API key is sent only as a header.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta
from typing import Callable

import requests

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODELS = ["models/gemini-3.5-flash-lite", "models/gemini-flash-lite-latest", "models/gemini-3.1-flash-lite"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; guru-media/1.0)"}
NOTE = ("Gemini description of the attached image(s). Context only: not the tracked person's words, "
        "never evidence for stance, freshness, convergence, or alerts.")
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "images": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "image_text": {"type": "STRING"},
            "what_it_shows": {"type": "STRING"},
            "chart_credit": {"type": "STRING"},
        }, "required": ["image_text", "what_it_shows", "chart_credit"]}},
    },
    "required": ["images"],
}
PROMPT = ("These are image(s) attached to an X post by {person} (@{handle}). The post text is: \"{text}\"\n"
          "For each image, in order:\n"
          "- image_text: the visible text in the image (titles, axis labels, legends, annotations, watermarks), "
          "verbatim, max 400 characters.\n"
          "- what_it_shows: one or two factual sentences on what the chart/screenshot shows (series, period, the "
          "latest reading or move if visible). Describe only what is visible. Do not forecast, do not give an "
          "opinion, do not say what the poster thinks.\n"
          "- chart_credit: the chart's maker if visible (watermark, handle, logo, source line), else \"unknown\".")


def _img(url: str, get: Callable) -> tuple[str, bytes] | None:
    u = url + ("&" if "?" in url else "?") + "name=small" if "pbs.twimg.com/media/" in url else url
    r = get(u)
    if r.status_code != 200 or not r.content:
        return None
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if not mime.startswith("image/"):
        return None
    return mime, r.content


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


def describe_posts(posts: list[dict], state: dict, now: datetime, key: str | None, cfg: dict,
                   get: Callable | None = None, post: Callable | None = None,
                   sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> dict:
    get = get or (lambda u: requests.get(u, headers=UA, timeout=30))
    post = post or (lambda url, body, k: requests.post(url, json=body, timeout=120,
                                                       headers={"x-goog-api-key": k, "Content-Type": "application/json"}))
    models = cfg.get("models", DEFAULT_MODELS)
    day = now.strftime("%Y-%m-%d")
    usage = state.get("media_vision") or {}
    if usage.get("date") != day:
        usage = {"date": day, "calls": 0}
    daily_cap, run_cap = cfg.get("daily_cap", 40), cfg.get("run_cap", 25)
    max_age = timedelta(hours=cfg.get("max_age_hours", 72))
    budget, t0 = cfg.get("time_budget_sec", 300), clock()
    stats = {"status": "ok" if key else "no key", "described": 0, "pending": 0, "errors": 0,
             "calls_today": usage["calls"], "daily_cap": daily_cap, "note": NOTE}

    def images_of(p: dict) -> list[dict]:
        own = [dict(m, of="post") for m in p.get("media") or []]
        quoted = [dict(m, of="quoted_post") for m in (p.get("quoted_post") or {}).get("media") or []]
        return (own + quoted)[:4]

    todo = [p for p in posts if images_of(p) and not (p.get("media_summary") or {}).get("status") == "ready"
            and (p.get("media_summary") or {}).get("tries", 0) < 3
            and now - datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) <= max_age]
    todo.sort(key=lambda p: (p.get("tier", 9), -datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).timestamp()))
    done = 0
    for p in todo:
        if not key or usage["calls"] >= daily_cap or done >= run_cap or clock() - t0 > budget:
            stats["pending"] += 1
            continue
        imgs = images_of(p)
        parts = [{"text": PROMPT.format(person=p.get("person"), handle=p.get("handle"), text=(p.get("text") or "")[:600])}]
        got = []
        for m in imgs:
            try:
                im = _img(m["url"], get)
            except Exception:
                im = None
            if im:
                parts.append({"inline_data": {"mime_type": im[0], "data": base64.b64encode(im[1]).decode()}})
                got.append(m)
        ms = p.get("media_summary") or {}
        if not got:
            p["media_summary"] = {"status": "image_unavailable", "tries": ms.get("tries", 0) + 1, "note": NOTE}
            stats["errors"] += 1
            continue
        try:
            usage["calls"] += 1
            res, model = _call(key, models, parts, post, sleep)
        except Exception as e:
            p["media_summary"] = {"status": "error", "tries": ms.get("tries", 0) + 1, "error": str(e)[:80], "note": NOTE}
            stats["errors"] += 1
            if str(e) == "quota":
                key = None
                stats["status"] = "quota"
            continue
        out = []
        for m, d in zip(got, res.get("images") or []):
            out.append({"of": m["of"], "kind": m["kind"], "url": m["url"],
                        "image_text": (d.get("image_text") or "")[:400],
                        "what_it_shows": (d.get("what_it_shows") or "")[:300],
                        "chart_credit": (d.get("chart_credit") or "unknown")[:80]})
        p["media_summary"] = {"status": "ready", "model": model, "at": now.isoformat(timespec="seconds"),
                              "images": out, "note": NOTE}
        stats["described"] += 1
        done += 1
        sleep(1)
    state["media_vision"] = usage
    stats["calls_today"] = usage["calls"]
    return stats
