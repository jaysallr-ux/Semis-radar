"""Which YouTube metadata source works from Actions: watch page, oEmbed, innertube player?"""
import json, re
from pathlib import Path
import requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
IDS = ["jdRH4nXvmpE", "7WFf4ryfZeY", "X4ehXm5FnLM", "ffiSgUoDNbQ", "aRwlDuI2jLQ"]
out = {}
for vid in IDS:
    r = {}
    try:
        w = requests.get(f"https://www.youtube.com/watch?v={vid}", headers=UA, timeout=25)
        r["watch"] = {"http": w.status_code, "len": len(w.text), "has_uploadDate": '"uploadDate"' in w.text, "has_channelId": '"channelId"' in w.text,
                      "bot_check": "confirm you" in w.text.lower(), "consent": "consent.youtube" in w.url}
    except Exception as e:
        r["watch"] = str(e)[:80]
    try:
        o = requests.get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, headers=UA, timeout=25)
        r["oembed"] = {"http": o.status_code, **({k: o.json().get(k) for k in ("title", "author_name", "author_url")} if o.status_code == 200 else {})}
    except Exception as e:
        r["oembed"] = str(e)[:80]
    try:
        body = {"videoId": vid, "context": {"client": {"clientName": "WEB", "clientVersion": "2.20241001.00.00", "hl": "en", "gl": "US"}}}
        p = requests.post("https://www.youtube.com/youtubei/v1/player?prettyPrint=false", json=body, headers=UA, timeout=25)
        j = p.json() if p.status_code == 200 else {}
        vd, mf = j.get("videoDetails", {}), j.get("microformat", {}).get("playerMicroformatRenderer", {})
        r["player"] = {"http": p.status_code, "status": j.get("playabilityStatus", {}).get("status"), "channelId": vd.get("channelId"), "title": vd.get("title"),
                       "publishDate": mf.get("publishDate"), "uploadDate": mf.get("uploadDate"), "lengthSeconds": vd.get("lengthSeconds")}
    except Exception as e:
        r["player"] = str(e)[:80]
    out[vid] = r
Path("guru_web/probe/meta_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
