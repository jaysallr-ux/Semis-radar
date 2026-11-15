"""One-off probe: can GitHub Actions read public X mirrors (with replies)? Prints status only; stores nothing."""
import re, sys, requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
user = sys.argv[1] if len(sys.argv) > 1 else "dampedspring"
urls = [f"https://twstalker.com/{user}", f"https://xcancel.com/{user}/with_replies", f"https://xcancel.com/{user}/rss",
        f"https://nitter.net/{user}/with_replies", f"https://nitter.net/{user}/rss", f"https://nitter.poast.org/{user}/with_replies",
        f"https://nitter.privacyredirect.com/{user}/with_replies", f"https://lightbrd.com/{user}/with_replies",
        f"https://nitter.tiekoetter.com/{user}/with_replies", f"https://twiiit.com/{user}", f"https://rsshub.app/twitter/user/{user}"]
for u in urls:
    try:
        r = requests.get(u, headers=H, timeout=25, allow_redirects=True)
        t = r.text
        title = (re.search(r"<title>(.*?)</title>", t, re.S | re.I) or [None, ""])[1].strip()[:80]
        ids = len(set(re.findall(r"/status/(\d{15,20})", t)))
        print(f"{r.status_code} {len(t):>7}B ids={ids:>3} 42069={'42069' in t} cf={'cloudflare' in t.lower() or 'cf-chl' in t} {u} -> {r.url[:60]} | {title}")
    except Exception as e:
        print(f"ERR {type(e).__name__} {u}")
