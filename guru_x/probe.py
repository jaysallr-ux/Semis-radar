"""무료 X 수집 경로 탐색용 1회성 프로브. 결과는 guru_x/data/probe.json.

1. 프로필 임베드 타임라인 (syndication.twitter.com/srv/timeline-profile) 에서 최근 글 ID 가 나오는지, 얼마나 최신인지
2. syndication tweet-result (cdn.syndication.twimg.com) 로 글 1개의 전문/작성자/시각이 나오는지
3. oEmbed 대조
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
HANDLES = ["jam_croissant", "dampedspring", "RyanDetrick", "MarkNewtonCMT"]
KNOWN_ID = "2111801778551259342"
OUT = Path(__file__).resolve().parent / "data" / "probe.json"
EPOCH = 1288834974657


def sf_time(pid: str) -> str:
    return datetime.fromtimestamp(((int(pid) >> 22) + EPOCH) / 1000, tz=timezone.utc).isoformat(timespec="minutes")


def to_base36(x: float) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip, fp = int(x), x - int(x)
    s = ""
    while ip:
        ip, r = divmod(ip, 36)
        s = digits[r] + s
    s = s or "0"
    if fp:
        s += "."
        for _ in range(12):
            fp *= 36
            d = int(fp)
            s += digits[d]
            fp -= d
            if not fp:
                break
    return s


def syndication_token(pid: str) -> str:
    return re.sub(r"(0+|\.)", "", to_base36((int(pid) / 1e15) * math.pi))


def probe_timeline(h: str) -> dict:
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{h}"
    try:
        r = requests.get(url, headers=UA, timeout=30)
    except Exception as e:
        return {"error": str(e)}
    out = {"status": r.status_code, "bytes": len(r.text)}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    ids = sorted(set(re.findall(r'"id_str":"(\d{15,25})"', r.text)), key=int, reverse=True)
    out["next_data"] = bool(m)
    entries = []
    if m:
        try:
            d = json.loads(m.group(1))
            tl = (((d.get("props") or {}).get("pageProps") or {}).get("timeline") or {}).get("entries") or []
            for e in tl:
                t = ((e.get("content") or {}).get("tweet")) or {}
                if t.get("id_str"):
                    entries.append({"id": t["id_str"], "created_at": t.get("created_at"),
                                    "screen_name": (t.get("user") or {}).get("screen_name"),
                                    "text": (t.get("full_text") or t.get("text") or "")[:120],
                                    "is_pinned": bool(e.get("content", {}).get("tweet", {}).get("is_pinned"))})
        except Exception as ex:
            out["parse_error"] = str(ex)
    out["entries_count"] = len(entries)
    out["entries_top5"] = entries[:5]
    out["regex_ids_count"] = len(ids)
    own = [e for e in entries if (e["screen_name"] or "").lower() == h.lower()]
    newest = max((int(e["id"]) for e in own), default=None)
    out["own_entries"] = len(own)
    out["newest_own_post_time"] = sf_time(str(newest)) if newest else None
    out["newest_any_id_time"] = sf_time(ids[0]) if ids else None
    out["snippet"] = r.text[:300] if not m else None
    return out


def probe_tweet_result(pid: str) -> dict:
    url = "https://cdn.syndication.twimg.com/tweet-result"
    try:
        r = requests.get(url, params={"id": pid, "lang": "en", "token": syndication_token(pid)}, headers=UA, timeout=30)
    except Exception as e:
        return {"error": str(e)}
    out = {"status": r.status_code}
    try:
        d = r.json()
    except Exception:
        out["snippet"] = r.text[:300]
        return out
    out["keys"] = sorted(d.keys())[:40]
    out["screen_name"] = (d.get("user") or {}).get("screen_name")
    out["created_at"] = d.get("created_at")
    out["text"] = (d.get("text") or "")[:300]
    out["has_note_tweet"] = "note_tweet" in d
    out["note_tweet_text"] = (((d.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text", "")[:300] if d.get("note_tweet") else None
    out["in_reply_to_screen_name"] = d.get("in_reply_to_screen_name")
    out["quoted"] = bool(d.get("quoted_tweet"))
    return out


def probe_oembed(pid: str, h: str) -> dict:
    try:
        r = requests.get("https://publish.twitter.com/oembed",
                         params={"url": f"https://twitter.com/{h}/status/{pid}", "omit_script": "true"}, timeout=20)
        d = r.json()
        return {"status": r.status_code, "author_url": d.get("author_url"), "html": (d.get("html") or "")[:300]}
    except Exception as e:
        return {"error": str(e)}


def main() -> None:
    res = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "timelines": {}, "tweet_result": {}}
    newest_ids = {}
    for h in HANDLES:
        t = probe_timeline(h)
        res["timelines"][h] = t
        if t.get("entries_top5"):
            own = [e for e in t["entries_top5"] if (e.get("screen_name") or "").lower() == h.lower()]
            if own:
                newest_ids[h] = max(own, key=lambda e: int(e["id"]))["id"]
    res["tweet_result"]["known_" + KNOWN_ID] = probe_tweet_result(KNOWN_ID)
    for h, pid in newest_ids.items():
        res["tweet_result"][f"{h}_{pid}"] = probe_tweet_result(pid)
    res["oembed_known"] = probe_oembed(KNOWN_ID, "RyanDetrick")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:6000])


if __name__ == "__main__":
    main()
