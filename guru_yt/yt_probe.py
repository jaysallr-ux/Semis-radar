"""1회성 프로브: GitHub Actions 에서 YouTube 자막과 채널 RSS 가 받아지는지 확인. 결과는 guru_yt/data/yt_probe.json."""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "data" / "yt_probe.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
KNOWN = {"karsan_overtime": "YIR7RVLadt8", "bianco_monetary_matters": "bygJZ4o0SJE", "constan_excess_returns": "-nDxML0csuM"}
CHANNEL_HANDLES = ["42macro", "42Macro", "SchwabNetwork", "ExcessReturnsPod", "MonetaryMattersNetwork", "CNBCtelevision", "Morningstar"]
ROUNDS = 3


def channel_rss(handle: str) -> dict:
    out = {"handle": handle}
    try:
        r = requests.get(f"https://www.youtube.com/@{handle}", headers=UA, timeout=30)
        out["page_status"] = r.status_code
        m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', r.text) or re.search(r'channel/(UC[\w-]{22})', r.text)
        if not m:
            out["channel_id"] = None
            out["page_hint"] = "consent" if "consent.youtube.com" in r.url or "Before you continue" in r.text else r.text[:120]
            return out
        cid = m.group(1)
        out["channel_id"] = cid
        f = requests.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": cid}, headers=UA, timeout=30)
        out["rss_status"] = f.status_code
        vids = re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>\s*<yt:channelId>[\w-]+</yt:channelId>\s*<title>(.*?)</title>", f.text, re.S)
        pubs = re.findall(r"<published>(.*?)</published>", f.text)[1:]
        out["rss_count"] = len(vids)
        out["rss_latest"] = [{"id": v, "title": t[:90], "published": p} for (v, t), p in list(zip(vids, pubs))[:3]]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def transcript(video_id: str) -> dict:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        t0 = time.time()
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        snippets = list(fetched)
        text = " ".join(s.text for s in snippets)
        return {"ok": True, "language": getattr(fetched, "language_code", None),
                "is_generated": getattr(fetched, "is_generated", None), "chars": len(text),
                "seconds": round(time.time() - t0, 1), "sample": text[:120]}
    except Exception as e:
        return {"ok": False, "error_type": type(e).__name__, "error": str(e).strip().splitlines()[0][:200] if str(e).strip() else repr(e)[:200]}


def main() -> None:
    res = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "channels": [], "transcripts": {}}
    for h in CHANNEL_HANDLES:
        res["channels"].append(channel_rss(h))
        time.sleep(2)
    targets = dict(KNOWN)
    for c in res["channels"]:
        if c["handle"].lower() == "42macro" and c.get("rss_latest"):
            targets["dale_42macro_latest"] = c["rss_latest"][0]["id"]
            break
    for rnd in range(1, ROUNDS + 1):
        for name, vid in targets.items():
            res["transcripts"].setdefault(name, {"video_id": vid, "rounds": []})
            res["transcripts"][name]["rounds"].append(transcript(vid))
            time.sleep(5)
        if rnd < ROUNDS:
            time.sleep(25)
    for name, v in res["transcripts"].items():
        v["success_rounds"] = sum(1 for r in v["rounds"] if r.get("ok"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:8000])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
