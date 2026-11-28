import json, os, re, requests
OUT = os.path.join(os.path.dirname(__file__), "data", "probe")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (personal research)"}
log = {}
def get(name, url, fn):
	try:
		r = requests.get(url, timeout=90, headers=UA)
		log[name] = {"url": url, "status": r.status_code, "bytes": len(r.content)}
		if r.ok and fn:
			with open(os.path.join(OUT, fn), "wb") as f:
				f.write(r.content)
		return r
	except Exception as e:
		log[name] = {"url": url, "error": str(e)}
import datetime as dt
r = get("bc_supply", "https://api.blockchain.info/charts/total-bitcoins?timespan=all&format=json&sampled=false", None)
if r is not None and r.ok:
	daily = {}
	for p in r.json()["values"]:
		daily[dt.datetime.fromtimestamp(p["x"], dt.timezone.utc).date().isoformat()] = p["y"]
	with open(os.path.join(OUT, "supply_daily_eod.json"), "w") as f:
		json.dump(daily, f, separators=(",", ":"))
idx = get("coc_index", "https://charts.checkonchain.com/", "coc_index.html")
links = []
if idx is not None and idx.ok:
	links = sorted(set(re.findall(r'href="([^"]+\.html)"', idx.text)))
	with open(os.path.join(OUT, "coc_links.txt"), "w") as f:
		f.write("\n".join(links))
hits = [l for l in links if re.search(r"originals|balanc|cvdd", l, re.I)]
log["coc_hits"] = hits
for i, l in enumerate(hits[:12]):
	url = l if l.startswith("http") else "https://charts.checkonchain.com/" + l.lstrip("/")
	r = get(f"coc_{i}", url, f"coc_{i}.html")
	if r is not None and r.ok:
		names = sorted(set(re.findall(r'"name":"([^"]{1,80})"', r.text)))
		log[f"coc_{i}"]["trace_names"] = names
with open(os.path.join(OUT, "probe_log.json"), "w") as f:
	json.dump(log, f, indent=1)
print(json.dumps(log, indent=1))
