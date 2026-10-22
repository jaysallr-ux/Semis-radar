"""One-off: find correct channel IDs by YouTube channel search, then check RSS size and latest titles."""
import json, re, requests
from pathlib import Path
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
QUERIES = ["Monetary Matters Jack Farley", "MacroVoices Erik Townsend", "Fundstrat Global Advisors", "FS Insight Fundstrat"]
out = {}
for q in QUERIES:
    r = requests.get("https://www.youtube.com/results", params={"search_query": q, "sp": "EgIQAg=="}, headers=UA, timeout=60)
    res = []
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text)
    if m:
        data = json.loads(m.group(1))
        def walk(o):
            if isinstance(o, dict):
                if "channelRenderer" in o:
                    c = o["channelRenderer"]
                    res.append({"id": c.get("channelId"), "title": c.get("title", {}).get("simpleText"),
                                "handle": (c.get("subscriberCountText") or {}).get("simpleText"),
                                "subs": (c.get("videoCountText") or {}).get("simpleText")})
                for v in o.values(): walk(v)
            elif isinstance(o, list):
                for v in o: walk(v)
        walk(data)
    for c in res[:4]:
        rr = requests.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": c["id"]}, headers=UA, timeout=60)
        titles = re.findall(r"<title>(.*?)</title>", rr.text)[1:4]
        dates = re.findall(r"<published>(.*?)</published>", rr.text)[1:2]
        c.update(rss_status=rr.status_code, rss_entries=rr.text.count("<entry>"), latest=titles, latest_date=dates)
    out[q] = {"http": r.status_code, "channels": res[:4]}
Path("guru_yt/probe/channel_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
