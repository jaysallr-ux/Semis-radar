"""Probe 2: get real publisher URLs and article text.
a) Bing News RSS (links carry the publisher URL in a url= parameter)
b) Google News link decoding via the public batchexecute endpoint
c) author fields in Carson / FS Insight / Unlimited / Damped Spring feeds"""
import json, re, time, urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
import requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
Q = {"Mark Newton": '"Mark Newton" Fundstrat', "Warren Pies": '"Warren Pies"', "Cem Karsan": '"Cem Karsan"', "Bob Elliott": '"Bob Elliott"', "Andy Constan": '"Andy Constan"'}
out = {"bing": {}, "gnews_decode": {}, "feeds": {}}
def get(u, **kw):
    try:
        r = requests.get(u, headers=UA, timeout=25, **kw); return r.status_code, r.text, r.url
    except Exception as e:
        return None, type(e).__name__, u
def article(url, last):
    st, html, final = get(url)
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text); text = re.sub(r"\s+", " ", text)
    quotes = re.findall(r"[\"“]([^\"”]{40,300})[\"”]", text)
    return {"url": final[:200], "http": st, "text_chars": len(text), "name_in_text": last in text.lower(), "quote_like": len(quotes), "q_sample": quotes[:2]}
for p, q in Q.items():
    st, x, _ = get("https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "rss", "qft": 'interval="8"'}))
    rec = {"http": st, "items": []}
    try:
        root = ET.fromstring(x.encode())
        for it in root.findall(".//item")[:6]:
            link = it.findtext("link") or ""
            m = re.search(r"[?&]url=([^&]+)", link)
            real = urllib.parse.unquote(m.group(1)) if m else link
            rec["items"].append({"title": (it.findtext("title") or "")[:100], "date": it.findtext("pubDate"), "real": real[:200]})
    except Exception as e:
        rec["parse_error"] = str(e)[:100]
    for it in rec["items"][:2]:
        it["article"] = article(it["real"], p.split()[-1].lower()); time.sleep(1)
    out["bing"][p] = rec
    time.sleep(1.5)
# google news decode
st, x, _ = get("https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": '"Warren Pies" when:7d', "hl": "en-US", "gl": "US", "ceid": "US:en"}))
links = re.findall(r"<link>(https://news\.google\.com/rss/articles/[^<]+)</link>", x or "")[:2]
for gl in links:
    rec = {"link": gl[:120]}
    aid = gl.split("/articles/")[1].split("?")[0]
    st2, page, _ = get(f"https://news.google.com/articles/{aid}")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page or ""); ts = re.search(r'data-n-a-ts="([^"]+)"', page or "")
    rec["page_http"], rec["has_sig"] = st2, bool(sg and ts)
    if sg and ts:
        payload = ["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]']
        try:
            r = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                              data="f.req=" + urllib.parse.quote(json.dumps([[payload]])), timeout=25)
            m = re.search(r'\\"garturlres\\",\\"(https?://[^\\"]+)', r.text)
            rec["decode_http"], rec["decoded"] = r.status_code, (m.group(1)[:200] if m else None)
            if m:
                rec["article"] = article(m.group(1), "pies")
        except Exception as e:
            rec["decode_error"] = type(e).__name__
    out["gnews_decode"][aid[:20]] = rec
    time.sleep(1)
for name, u in {"carson": "https://www.carsongroup.com/feed", "fsinsight": "https://fsinsight.com/feed", "unlimited": "https://www.unlimitedfunds.com/feed", "dampedspring": "https://www.dampedspring.com/feed"}.items():
    st, x, _ = get(u)
    try:
        root = ET.fromstring(x.encode())
        out["feeds"][name] = [{"title": (it.findtext("title") or "")[:90], "date": it.findtext("pubDate"), "author": it.findtext("{http://purl.org/dc/elements/1.1/}creator"),
                               "cats": [c.text for c in it.findall("category")][:4]} for it in root.findall(".//item")[:8]]
    except Exception as e:
        out["feeds"][name] = {"http": st, "error": str(e)[:100]}
Path("guru_web/probe/web_probe2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
