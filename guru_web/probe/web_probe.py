"""One-off probe from GitHub Actions: which free web sources work for the 11 tracked people?
1) Google News RSS per person (+ can we resolve the article link and fetch the article?)
2) Candidate newsletter/blog RSS feeds on the people's own domains"""
import json, re, time, urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
PEOPLE = {
    "Mark Newton": '"Mark Newton" Fundstrat', "Cem Karsan": '"Cem Karsan"', "Warren Pies": '"Warren Pies"',
    "Darius Dale": '"Darius Dale"', "Ryan Detrick": '"Ryan Detrick"', "David Sekera": 'Sekera Morningstar',
    "Andy Constan": '"Andy Constan"', "Marko Papic": '"Marko Papic"', "Jim Bianco": '"Jim Bianco"',
    "Bob Elliott": '"Bob Elliott" Unlimited', "Lyn Alden": '"Lyn Alden"',
}
SITES = {
    "Lyn Alden": ["https://www.lynalden.com"], "Bob Elliott": ["https://www.unlimitedfunds.com", "https://bobelliott.substack.com"],
    "Darius Dale": ["https://42macro.com"], "Andy Constan": ["https://www.dampedspring.com", "https://dampedspring.substack.com"],
    "Jim Bianco": ["https://www.biancoresearch.com"], "Ryan Detrick": ["https://www.carsongroup.com"],
    "Mark Newton": ["https://fsinsight.com", "https://www.fundstrat.com"], "Warren Pies": ["https://www.3fourteenresearch.com"],
    "Cem Karsan": ["https://www.kaivolatility.com"], "Marko Papic": ["https://www.clocktowergroup.com", "https://markopapic.substack.com"],
    "David Sekera": ["https://www.morningstar.com"],
}
PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/blog/feed/", "/insights/feed/"]

def get(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=25, **kw)
        return r.status_code, r.text, r.url
    except Exception as e:
        return None, f"{type(e).__name__}", url

def parse_items(xml):
    try:
        root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    except Exception:
        return None
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in items[:30]:
        t = it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or ""
        d = it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated") or ""
        l = it.findtext("link") or ""
        if not l:
            le = it.find("{http://www.w3.org/2005/Atom}link")
            l = le.get("href") if le is not None else ""
        src = it.findtext("source") or ""
        body = it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or ""
        out.append({"title": t.strip()[:160], "date": d, "link": l, "source": src, "body_chars": len(body)})
    return out

res = {"news": {}, "sites": {}}
for person, q in PEOPLE.items():
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    st, text, _ = get(url)
    items = parse_items(text) if st == 200 else None
    rec = {"http": st, "count": len(items) if items is not None else None, "sample": (items or [])[:5]}
    # try resolving first two links to the publisher and fetching the article
    tries = []
    for it in (items or [])[:2]:
        st2, html, final = get(it["link"], allow_redirects=True)
        m = re.search(r'data-n-au="([^"]+)"', html or "") or re.search(r'<a[^>]+href="(https?://(?!news\.google)[^"]+)"', html or "")
        target = final if final and "news.google.com" not in final else (m.group(1) if m else None)
        art = None
        if target:
            st3, ahtml, _ = get(target)
            last = person.split()[-1].lower()
            art = {"url": target[:200], "http": st3, "chars": len(ahtml or ""), "name_in_page": last in (ahtml or "").lower()}
        tries.append({"gnews_http": st2, "final_is_google": "news.google.com" in (final or ""), "publisher": art})
        time.sleep(1)
    rec["resolve"] = tries
    res["news"][person] = rec
    time.sleep(1.5)

for person, bases in SITES.items():
    found = []
    for b in bases:
        for p in PATHS:
            st, text, final = get(b + p)
            if st == 200 and ("<rss" in text[:500] or "<feed" in text[:500] or "<?xml" in text[:100]):
                items = parse_items(text) or []
                found.append({"url": b + p, "items": len(items), "latest": items[:3]})
                break
        else:
            st, _, _ = get(b)
            found.append({"base": b, "home_http": st, "feed": None})
    res["sites"][person] = found

Path("guru_web/probe/web_probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=2))
