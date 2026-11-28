import json, os, re, time, html, requests
PROBE_VERSION = 4
import gzip
OUT = os.path.join(os.path.dirname(__file__), "data", "sec")
os.makedirs(OUT, exist_ok=True)
UA_STR = os.environ.get("SEC_USER_AGENT", "").strip()
if not UA_STR:
	os.makedirs(OUT, exist_ok=True)
	json.dump({"error": "SEC_USER_AGENT secret not set (SEC requires a declared name + real contact email)"}, open(os.path.join(OUT, "log.json"), "w"), indent=1)
	print("SEC_USER_AGENT not set; skipping")
	raise SystemExit(0)
UA = {"User-Agent": UA_STR, "Accept-Encoding": "gzip, deflate"}
CIK = "0001050446"
log = {}
def get(url, **kw):
	for i in range(4):
		r = requests.get(url, headers=UA, timeout=60, **kw)
		if r.status_code in (429, 503):
			time.sleep(2 + i * 3)
			continue
		return r
	return r
r = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json")
log["companyfacts"] = r.status_code
if r.ok:
	open(os.path.join(OUT, "companyfacts.json"), "wb").write(r.content)
r = get(f"https://data.sec.gov/submissions/CIK{CIK}.json")
log["submissions"] = r.status_code
if not r.ok:
	log["submissions_body_head"] = r.text[:400]
	json.dump(log, open(os.path.join(OUT, "log.json"), "w"), indent=1)
	print(json.dumps(log, indent=1))
	raise SystemExit(0)
sub = r.json()
filings = []
def add(block):
	for i in range(len(block["form"])):
		filings.append({k: block[k][i] for k in ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "primaryDocument"]})
add(sub["filings"]["recent"])
for f in sub["filings"].get("files", []):
	rr = get("https://data.sec.gov/submissions/" + f["name"])
	if rr.ok:
		add(rr.json())
	time.sleep(0.2)
json.dump(filings, open(os.path.join(OUT, "filings.json"), "w"))
want = [f for f in filings if f["filingDate"] >= "2020-08-01" and f["form"] in ("8-K", "8-K/A", "10-Q", "10-K")]
log["n_filings_since_2020_08"] = len(want)
SENT = re.compile(r"[^.]{0,400}(?:bitcoin|BTC|shares of (?:class a )?common stock|MSTR|ATM|aggregate)[^.]{0,400}\.", re.I)
snips = {}
for f in want:
	acc = f["accessionNumber"].replace("-", "")
	base = f"https://www.sec.gov/Archives/edgar/data/1050446/{acc}/"
	idx = get(base + "index.json")
	time.sleep(0.12)
	if not idx.ok:
		snips[f["accessionNumber"]] = {"meta": f, "error": idx.status_code}
		continue
	items = [it["name"] for it in idx.json()["directory"]["item"]]
	docs = [n for n in items if n.lower().endswith((".htm", ".html", ".txt")) and ("ex99" in n.lower() or "ex-99" in n.lower() or n == f["primaryDocument"])]
	if f["form"] in ("10-Q", "10-K"):
		docs = [f["primaryDocument"]]
	texts = []
	for dname in docs[:4]:
		dr = get(base + dname)
		time.sleep(0.12)
		if not dr.ok:
			continue
		t = re.sub(r"<[^>]+>", " ", dr.text)
		t = html.unescape(t)
		t = re.sub(r"\s+", " ", t)
		if f["form"] in ("10-Q", "10-K"):
			i = [m.start() for m in re.finditer(r"bitcoin|registrant had|common stock outstanding|Convertible Notes|liquidation preference", t, re.I)]
			keep = [t[max(0, j - 400): j + 600] for j in i[:300]]
		else:
			keep = [t[:60000]]
		texts.append({"doc": dname, "snips": keep, "len": len(t)})
	snips[f["accessionNumber"]] = {"meta": f, "docs": texts}
with gzip.open(os.path.join(OUT, "filing_text.json.gz"), "wt") as g:
	json.dump(snips, g)
log["snips"] = len(snips)
json.dump(log, open(os.path.join(OUT, "log.json"), "w"), indent=1)
print(json.dumps(log, indent=1))
