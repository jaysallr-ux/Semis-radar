import json, re, sys
from datetime import datetime, timezone
import requests
LIST_ID = "2115232721006137552"
URL = f"https://syndication.twitter.com/srv/timeline-list/list-id/{LIST_ID}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
out = {"checked_at": datetime.now(timezone.utc).isoformat(), "list_id": LIST_ID}
r = requests.get(URL, headers=UA, timeout=30)
out["status"] = r.status_code
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
posts = []
if m:
    try:
        entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
    except Exception as e:
        entries = []
        out["parse_error"] = str(e)
    for e in entries:
        t = (e.get("content") or {}).get("tweet") or {}
        if not t.get("id_str"):
            continue
        posts.append({
            "id": t["id_str"],
            "user": (t.get("user") or {}).get("screen_name"),
            "created_at": t.get("created_at"),
            "is_retweet": bool(t.get("retweeted_status")),
            "is_reply": bool(t.get("in_reply_to_status_id_str")),
            "text": t.get("full_text") or t.get("text"),
            "media": len(((t.get("extended_entities") or t.get("entities") or {}).get("media")) or []),
        })
out["count"] = len(posts)
out["posts"] = posts
json.dump(out, open("kkeujeok/probe.json", "w"), ensure_ascii=False, indent=1)
print(out["status"], out["count"])
