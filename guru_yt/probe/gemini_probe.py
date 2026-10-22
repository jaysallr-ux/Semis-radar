"""One-off probe: can Gemini API read public YouTube URLs from GitHub Actions and give
verbatim, timestamped quotes accurate enough to count as first-hand evidence?

Accuracy is checked against YouTube captions we already downloaded (guru_yt/probe/gt/*.txt).
Captions are auto-generated, so they are a cross-check, not perfect ground truth.

The API key is read from GEMINI_API_KEY and sent only in the x-goog-api-key header.
It is never printed, never written to the output file, never put in a URL.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from rapidfuzz import fuzz

KEY = os.environ.get("GEMINI_API_KEY", "")
BASE = "https://generativelanguage.googleapis.com/v1beta"
HERE = Path(__file__).parent
OUT = HERE / "gemini_probe.json"

VIDEOS = [
    {"id": "rVz79aPJnqE", "person": "Darius Dale", "kind": "solo short"},
    {"id": "VvbPfe6qucU", "person": "Darius Dale", "kind": "solo short"},
    {"id": "-nDxML0csuM", "person": "Andy Constan", "kind": "long interview"},
]
PREFERRED = ["flash"]  # pick a flash model from the live model list; avoid pro on free tier


def scrub(s):
    s = str(s)
    return s.replace(KEY, "***") if KEY else s


def H():
    return {"x-goog-api-key": KEY, "Content-Type": "application/json"}


def list_models():
    r = requests.get(f"{BASE}/models", headers=H(), params={"pageSize": 200}, timeout=60)
    if r.status_code != 200:
        return r.status_code, scrub(r.text[:500]), []
    ms = [m for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    return 200, "", [m["name"] for m in ms]


ORDER = ["models/gemini-3.5-flash", "models/gemini-flash-latest", "models/gemini-2.5-flash",
         "models/gemini-3.8-flash", "models/gemini-3.6-flash", "models/gemini-3.7-flash"]


def pick_models(names):
    return [m for m in ORDER if m in names]


# ---------- ground truth ----------
def load_gt(vid):
    raw = (HERE / "gt" / f"{vid}.txt").read_text(encoding="utf-8")
    text, marks = [], []
    pos = 0
    for m in re.finditer(r"\[(\d+):(\d+)(?::(\d+))?\]\s*([^\[]*)", raw):
        a, b, c, seg = m.groups()
        secs = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
        seg = norm(seg)
        marks.append((pos, secs))
        text.append(seg)
        pos += len(seg) + 1
    return " ".join(text), marks


def norm(s):
    s = s.lower().replace("’", "'")
    s = re.sub(r"\[music\]|>>", " ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def ts_to_s(t):
    try:
        p = [int(x) for x in str(t).strip().split(":")]
    except ValueError:
        return None
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else (p[0] * 60 + p[1] if len(p) == 2 else None)


def score_quote(q, gt, marks):
    qt = norm(q.get("text", ""))
    if len(qt) < 15:
        return {"match": None}
    al = fuzz.partial_ratio_alignment(qt, gt)
    sec = None
    for p, s in marks:
        if p <= al.dest_start:
            sec = s
        else:
            break
    claimed = ts_to_s(q.get("t") or q.get("timestamp"))
    return {"match": round(al.score, 1), "gt_sec": sec, "claimed_sec": claimed,
            "ts_delta": (claimed - sec) if (claimed is not None and sec is not None) else None}


# ---------- gemini ----------
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "person_present": {"type": "BOOLEAN"},
        "person_speaks": {"type": "BOOLEAN"},
        "evidence": {"type": "STRING"},
        "quotes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "t": {"type": "STRING"}, "speaker": {"type": "STRING"}, "text": {"type": "STRING"}},
            "required": ["t", "speaker", "text"]}},
    },
    "required": ["person_present", "person_speaks", "evidence", "quotes"],
}


def quote_prompt(person):
    return (f"Tracked person: {person}.\n"
            "1. Does this person actually appear and speak in this video? Answer from what you hear, "
            "not from the title.\n"
            "2. Give 10 short quotes (1-3 sentences each) where THIS person states a view on markets, "
            "the economy, rates, stocks, credit or positioning. Copy the words exactly as spoken. "
            "Do not paraphrase, do not fix grammar, do not merge separate sentences. "
            "t = start time as M:SS or H:MM:SS. speaker = who is speaking.\n"
            "If the person does not speak, return person_speaks=false and an empty quotes list.")


def call(model, vid, prompt, mode, schema=SCHEMA, end=None):
    part = {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={vid}"}}
    gen = {"temperature": 0}
    if schema:
        gen.update(responseMimeType="application/json", responseSchema=schema)
    if mode == "static_low":
        part["videoMetadata"] = {"fps": 0.1}
        if end:
            part["videoMetadata"]["endOffset"] = end
        gen["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
    elif mode == "agentic":
        part["mediaProcessing"] = "AGENTIC"
    body = {"contents": [{"parts": [part, {"text": prompt}]}], "generationConfig": gen}
    t0 = time.time()
    attempts = []
    for a in range(3):  # 503 "high demand" is transient: back off and retry
        try:
            r = requests.post(f"{BASE}/{model}:generateContent", headers=H(), json=body, timeout=420)
        except Exception as e:
            attempts.append(f"exc {scrub(e)[:120]}")
            r = None
        else:
            attempts.append(r.status_code)
            if r.status_code not in (500, 503, 504):
                break
        time.sleep(30 * (a + 1))
    if r is None:
        return {"ok": False, "error": "all attempts failed", "attempts": attempts, "secs": round(time.time() - t0, 1)}
    res = {"http": r.status_code, "secs": round(time.time() - t0, 1), "attempts": attempts}
    if r.status_code != 200:
        res.update(ok=False, error=scrub(r.text[:800]))
        return res
    j = r.json()
    res["usage"] = j.get("usageMetadata")
    cand = (j.get("candidates") or [{}])[0]
    res["finish"] = cand.get("finishReason")
    txt = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if "text" in p)
    res["ok"] = bool(txt)
    res["text"] = txt
    return res


def run_quotes(model, v, mode, out):
    gt, marks = load_gt(v["id"])
    r = call(model, v["id"], quote_prompt(v["person"]), mode)
    rec = {"video": v["id"], "person": v["person"], "kind": v["kind"], "mode": mode, "model": model,
           **{k: r.get(k) for k in ("ok", "http", "secs", "attempts", "usage", "finish", "error")}}
    if r.get("ok"):
        try:
            d = json.loads(r["text"])
        except Exception:
            rec["parse_error"] = r["text"][:500]
            out.append(rec)
            return rec
        rec["person_present"] = d.get("person_present")
        rec["person_speaks"] = d.get("person_speaks")
        rec["evidence"] = d.get("evidence", "")[:300]
        qs = []
        for q in d.get("quotes", []):
            qs.append({"t": q.get("t"), "speaker": q.get("speaker"), "text": q.get("text", "")[:400],
                       **score_quote(q, gt, marks)})
        rec["quotes"] = qs
        ms = [q["match"] for q in qs if q.get("match") is not None]
        ds = [abs(q["ts_delta"]) for q in qs if q.get("ts_delta") is not None and (q.get("match") or 0) >= 85]
        rec["summary"] = {
            "n": len(qs),
            "verbatim_ge90": sum(m >= 90 for m in ms),
            "close_80_90": sum(80 <= m < 90 for m in ms),
            "fabricated_lt70": sum(m < 70 for m in ms),
            "median_match": sorted(ms)[len(ms) // 2] if ms else None,
            "ts_within_15s": sum(d <= 15 for d in ds),
            "ts_checked": len(ds),
            "median_ts_err": sorted(ds)[len(ds) // 2] if ds else None,
        }
    out.append(rec)
    return rec


def run_transcribe(model, vid, out):
    """Raw verbatim transcription of the first 3 minutes, compared to captions."""
    gt, marks = load_gt(vid)
    gt3 = gt[: next((p for p, s in marks if s >= 180), len(gt))]
    prompt = "Transcribe the speech verbatim from 0:00 to 3:00. Plain text only, no timestamps, no speaker labels."
    r = call(model, vid, prompt, "static_low", schema=None, end="180s")
    rec = {"video": vid, "test": "transcribe_first_3min", "model": model,
           **{k: r.get(k) for k in ("ok", "http", "secs", "attempts", "usage", "finish", "error")}}
    if r.get("ok"):
        g = norm(r["text"])
        rec["ratio_vs_captions"] = round(fuzz.ratio(g, gt3), 1)
        rec["len_gemini"], rec["len_captions"] = len(g), len(gt3)
        rec["sample"] = r["text"][:300]
    out.append(rec)


def main():
    out = {"ran_at": datetime.now(timezone.utc).isoformat(), "key_present": bool(KEY), "runs": []}
    if not KEY:
        out["fatal"] = "GEMINI_API_KEY secret is empty or not visible to this workflow"
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return
    st, err, names = list_models()
    out["list_models_http"], out["list_models_error"] = st, err
    out["models_available"] = names
    models = pick_models(names)
    out["models_tried_order"] = models
    if not models:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return

    model = None
    for m in models:  # find a model that accepts YouTube URLs at all, using the short video
        rec = run_quotes(m, VIDEOS[0], "static_low", out["runs"])
        if rec.get("ok"):
            model = m
            break
        time.sleep(10)
    out["model_used"] = model
    if model:
        plan = [(VIDEOS[1], "static_low"), (VIDEOS[2], "static_low"), (VIDEOS[2], "agentic")]
        for v, mode in plan:
            time.sleep(65)  # stay under free-tier tokens-per-minute
            run_quotes(model, v, mode, out["runs"])
        time.sleep(65)
        run_transcribe(model, VIDEOS[0]["id"], out["runs"])
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
