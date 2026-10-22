"""Probe 3: per person, Google News discovery -> decode publisher URL -> fetch body -> name/quote presence.
For video pages (CNBC/Yahoo video), also try to find the same segment on YouTube by title search."""
import json, re, time, urllib.parse
from pathlib import Path
import requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
PEOPLE = {"Mark Newton": '"Mark Newton" Fundstrat', "Cem Karsan": '"Cem Karsan"', "Warren Pies": '"Warren Pies"', "Darius Dale": '"Darius Dale" OR "42 Macro"',
          "Ryan Detrick": '"Ryan Detrick"', "David Sekera": '"Sekera" Morningstar', "Andy Constan": '"Andy Constan"', "Marko Papic": '"Marko Papic"',
          "Jim Bianco": '"Jim Bianco"', "Bob Elliott": '"Bob Elliott" Unlimited', "Lyn Alden": '"Lyn Alden"'}
def get(u, **kw):
    try:
        r = requests.get(u, headers=UA, timeout=25, **kw); return r.status_code, r.text, r.url
    except Exception as e:
        return None, type(e).__name__, u
def decode(link):
    aid = link.split("/articles/")[1].split("?")[0]
    st, page, _ = get(f"https://news.google.com/articles/{aid}")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page or ""); ts = re.search(r'data-n-a-ts="([^"]+)"', page or "")
    if not (sg and ts):
        return None
    payload = ["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]']
    try:
        r = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                          data="f.req=" + urllib.parse.quote(json.dumps([[payload]])), timeout=25)
        m = re.search(r'\\"garturlres\\",\\"(https?://[^\\"]+)', r.text)
        return m.group(1) if m else None
    except Exception:
        return None
def body(url, last):
    st, html, final = get(url)
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html or "", flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t); t = re.sub(r"\s+", " ", t)
    q = [x for x in re.findall(r"[\"“]([^\"”]{30,400})[\"”]", t)]
    return {"http": st, "chars": len(t), "name": last in t.lower(), "quotes": len(q), "q1": q[0][:120] if q else None}
def yt_search(title):
    st, html, _ = get("https://www.youtube.com/results", params={"search_query": title})
    m = re.findall(r'"videoId":"([\w-]{11})".{0,400}?"title":\{"runs":\[\{"text":"([^"]+)"', html or "")
    return [{"id": a, "title": b[:90]} for a, b in m[:2]]
out = {}
for p, q in PEOPLE.items():
    st, x, _ = get("https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}))
    items = re.findall(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>.*?<source[^>]*>(.*?)</source>", x or "", flags=re.S)
    last = p.split()[-1].lower()
    rows = []
    for title, link, date, src in items[:5]:
        real = decode(link)
        r = {"title": title[:110], "date": date[:16], "outlet": src, "decoded": bool(real), "url": (real or "")[:160]}
        if real:
            r["body"] = body(real, last)
            if "/video/" in real or "video" in real.split("/")[2]:
                r["youtube_match"] = yt_search(title.rsplit(" - ", 1)[0])
        rows.append(r)
        time.sleep(1.2)
    out[p] = {"http": st, "found_7d": len(items), "rows": rows}
Path("guru_web/probe/web_probe3.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
