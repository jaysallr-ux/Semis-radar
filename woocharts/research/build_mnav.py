import json, gzip, os, re, datetime as dt
H = os.path.dirname(__file__)
B = os.path.join(H, "..", "data")
S = json.load(gzip.open(os.path.join(B, "sec", "filing_text.json.gz"), "rt"))
CF = json.load(open(os.path.join(B, "sec", "companyfacts.json")))["facts"]["us-gaap"]
MSTR = json.load(open(os.path.join(B, "equities", "MSTR_1d.json")))["ohlc"]
coc = json.load(open(os.path.join(B, "probe", "coc_onchainoriginals.json")))["Price"]
HR = {x[:13]: y for x, y in zip(coc["x"], coc["y"]) if y}
MON = {m: i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"])}
def pdate(s):
	m = re.match(r"([A-Z][a-z]+) (\d{1,2}), ?(\d{4})", s)
	return dt.date(int(m.group(3)), MON[m.group(1)], int(m.group(2))).isoformat() if m and m.group(1) in MON else None
num = lambda s: int(s.replace(",", ""))
SPLIT = "2024-08-08"
filings = sorted(S.values(), key=lambda v: v["meta"]["acceptanceDateTime"])
ev = {"btc": [], "shares_anchor": [], "atm": [], "pref": [], "notes": []}
for f in filings:
	m = f["meta"]
	acc = m["acceptanceDateTime"][:19]
	t = " || ".join(x for d in f.get("docs", []) for x in d["snips"])
	got = None
	for mm in re.finditer(r"Aggregate BTC Holdings.{0,250}?Average Purchase Price \(\d\)\s+([\d,]+)\s+\$\s*[\d.,]+\s+\$\s*[\d,]+\s+([\d,]+)\s+\$", t):
		got = num(mm.group(2))
	if got is None:
		for mm in re.finditer(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4}),[^.|]{0,120}?(?:holds?|held)[^.|]{0,40}?(?:aggregate of )?(?:approximately )?([\d,]{4,}) bitcoins", t):
			got = num(mm.group(2))
	if got is None:
		mm = re.search(r"(?:holds?|held|purchased a total of)[^.|]{0,40}?(?:aggregate of )?(?:approximately )?([\d,]{4,}) bitcoins", t)
		if mm:
			got = num(mm.group(1))
	if got and m["form"].startswith("8-K"):
		ev["btc"].append({"acc": acc, "form": m["form"], "val": got})
	if m["form"] in ("10-Q", "10-K"):
		mm = re.search(r"As of ([A-Z][a-z]+ \d{1,2}, ?\d{4}) ?,? (?:the )?r ?egistrant had ([\d,]+) a ?nd ([\d,]+) shares of class A common stock and class B common stock", t)
		if mm:
			d0 = pdate(mm.group(1))
			v = num(mm.group(2)) + num(mm.group(3))
			if d0 < SPLIT:
				v *= 10
			ev["shares_anchor"].append({"acc": acc, "asof": d0, "val": v, "src": m["form"]})
		prefs = {}
		for mm in re.finditer(r"liquidation preference of (\$|€) ([\d,]+) at ([A-Z][a-z]+),? (\d{1,2}), ?(\d{4})", t):
			pre = t[max(0, mm.start() - 320):mm.start()]
			ks = re.findall(r"(Strike|Strife|Stride|Stretch|Stream)", pre, re.I)
			if ks:
				prefs.setdefault(ks[-1].title(), (pdate(f"{mm.group(3)} {mm.group(4)}, {mm.group(5)}"), mm.group(1), num(mm.group(2)) * 1000))
		if prefs:
			ev["pref"].append({"acc": acc, "src": "10-Q/K balance sheet text", "series": prefs})
	mm = re.search(r"Basic Shares Outstanding \(\d\)((?:\s+\d{1,3}(?:,\d{3})+)+)", t)
	if mm and m["form"].startswith("8-K"):
		vals = [num(x) for x in mm.group(1).split()]
		hdr = re.findall(r"(\d{1,2}/\d{2}/\d{4})", t[max(0, mm.start() - 1500):mm.start()])
		if vals and hdr:
			mo, dd, yy = hdr[-1].split("/")
			d0 = f"{yy}-{int(mo):02d}-{int(dd):02d}"
			ev["shares_anchor"].append({"acc": acc, "asof": d0, "val": vals[-1] * 1000, "src": "8-K BTC Yield table"})
	if m["form"].startswith("8-K"):
		w = re.search(r"(?:during the period|During Period)(?: between)? ([A-Z][a-z]+ \d{1,2}, \d{4}) (?:and|to) ([A-Z][a-z]+ \d{1,2}, \d{4})", t)
		wend = pdate(w.group(2)) if w else m["filingDate"]
		sold = None
		for rx in [r"MSTR Stock ([\d,]+) \$", r"Common ATM ([\d,]+) MSTR Shares", r"sold an aggregate of ([\d,]+) (?:Shares|shares of (?:its )?class A common stock)"]:
			mm = re.search(rx, t)
			if mm:
				sold = num(mm.group(1))
				break
		if sold:
			if wend and wend < SPLIT:
				sold *= 10
			ev["atm"].append({"acc": acc, "window_end": wend, "val": sold})
clean, rej = [], []
for x in ev["btc"]:
	if clean and x["val"] < 0.8 * clean[-1]["val"]:
		rej.append(x)
		continue
	clean.append(x)
ev["btc"] = clean
ev["btc_rejected"] = rej
def xbrl_series(tag, conv=1):
	pts = CF[tag]["units"]["USD"]
	byf = {}
	for p in pts:
		if p["form"] in ("10-Q", "10-K") and (p["filed"] not in byf or p["end"] > byf[p["filed"]]["end"]):
			byf[p["filed"]] = p
	return sorted([(f, byf[f]["end"], byf[f]["val"]) for f in byf])
fil_acc = {f["meta"]["filingDate"]: f["meta"]["acceptanceDateTime"][:19] for f in filings if f["meta"]["form"] in ("10-Q", "10-K")}
debt = [{"acc": fil_acc.get(f, f + "T21:00:00"), "end": e, "val": v} for f, e, v in xbrl_series("LongTermDebt") + [x for x in xbrl_series("ConvertibleLongTermNotesPayable") if x[0] < "2021-04-01"]]
debt.sort(key=lambda x: x["acc"])
cash = [{"acc": fil_acc.get(f, f + "T21:00:00"), "end": e, "val": v} for f, e, v in xbrl_series("CashAndCashEquivalentsAtCarryingValue")]
xpref = {fil_acc.get(f, f): v for f, e, v in xbrl_series("TemporaryEquityLiquidationPreference")}
pref = []
for p in ev["pref"]:
	usd = sum(v for d0, cur, v in p["series"].values() if cur == "$")
	eur = sum(v for d0, cur, v in p["series"].values() if cur == "€")
	val = xpref.get(p["acc"])
	src = "XBRL TemporaryEquityLiquidationPreference" if val else ("text sum (USD only)" if not eur else "text sum, EUR series missing FX")
	pref.append({"acc": p["acc"], "val": val if val else usd, "eur_missing": bool(eur) and not val, "src": src, "n_series": len(p["series"])})
for a, v in xpref.items():
	if not any(p["acc"] == a for p in pref):
		pref.append({"acc": a, "val": v, "src": "XBRL only"})
pref.sort(key=lambda x: x["acc"])
def asof(lst, cutoff):
	c = [x for x in lst if x["acc"] < cutoff]
	return c[-1] if c else None
def shares_at(cutoff):
	a = [x for x in ev["shares_anchor"] if x["acc"] < cutoff]
	if not a:
		return None, None
	anc = max(a, key=lambda x: (x["asof"], x["acc"]))
	add = sum(x["val"] for x in ev["atm"] if x["acc"] < cutoff and x["window_end"] and x["window_end"] > anc["asof"])
	return anc["val"] + add, anc
def is_edt(d):
	y = d.year
	m3 = dt.date(y, 3, 8) + dt.timedelta(days=(6 - dt.date(y, 3, 8).weekday()) % 7)
	n1 = dt.date(y, 11, 1) + dt.timedelta(days=(6 - dt.date(y, 11, 1).weekday()) % 7)
	return m3 <= d < n1
rows = []
for day in sorted(MSTR):
	if day < "2020-08-11":
		continue
	d = dt.date.fromisoformat(day)
	close_h = 20 if is_edt(d) else 21
	cutoff = f"{day}T{close_h:02d}:00:00"
	btc_px = HR.get(f"{day}T{close_h - 1:02d}") or HR.get(f"{day}T{close_h:02d}")
	b = asof(ev["btc"], cutoff)
	sh, anc = shares_at(cutoff)
	de = asof(debt, cutoff)
	ca = asof(cash, cutoff)
	pf = asof(pref, cutoff)
	r = {"date": day, "mstr": MSTR[day][3], "mstr_low": MSTR[day][2], "btc_px": btc_px, "btc_held": b["val"] if b else None, "shares": sh, "shares_anchor": anc["asof"] if anc else None, "debt": de["val"] if de else (0.0 if day < "2020-12-11" else None), "cash": ca["val"] if ca else None, "pref": pf["val"] if pf else (0.0 if day < "2025-01-31" else None), "missing": []}
	if not b:
		r["missing"].append("btc_held")
	if not sh:
		r["missing"].append("shares")
	if not ca:
		r["missing"].append("cash")
	if not btc_px:
		r["missing"].append("btc_px")
	if de is None and day >= "2020-12-11":
		r["missing"].append("debt_not_yet_in_10Q")
	if day >= "2025-01-31" and not pf:
		r["missing"].append("pref_not_yet_in_10Q")
	if pf and "2026-02-19" <= day < "2026-05-06":
		r["flags"] = ["pref_stale_FY2025_10K_not_parsed"]
	if not r["missing"]:
		nav = r["btc_held"] * btc_px
		mcap = r["mstr"] * sh
		evv = mcap + r["debt"] + r["pref"] - r["cash"]
		eqnav = nav - r["debt"] - r["pref"] + r["cash"]
		r.update({"btc_nav": nav, "mcap": mcap, "mnav_ev": evv / nav, "mnav_eq": mcap / eqnav if eqnav > 0 else None, "eq_nav_per_share": eqnav / sh})
	rows.append(r)
json.dump(rows, open(os.path.join(H, "mstr_mnav_daily.json"), "w"))
json.dump({"events": ev, "debt": debt, "cash": cash, "pref": pref}, open(os.path.join(H, "mnav_events.json"), "w"), indent=1)
with open(os.path.join(H, "mstr_mnav_daily.csv"), "w") as f:
	cols = ["date", "mstr", "mstr_low", "btc_px", "btc_held", "shares", "shares_anchor", "debt", "pref", "cash", "mnav_ev", "mnav_eq", "eq_nav_per_share", "missing"]
	f.write(",".join(cols) + "\n")
	for r in rows:
		f.write(",".join("" if r.get(c) is None else (";".join(r[c]) if c == "missing" else (f"{r[c]:.6g}" if isinstance(r[c], float) else str(r[c]))) for c in cols) + "\n")
print("btc events", len(ev["btc"]), "share anchors", len(ev["shares_anchor"]), "atm", len(ev["atm"]), "pref", len(pref), "debt", len(debt), "cash", len(cash))
for r in rows[::90] + rows[-3:]:
	print(r["date"], r["btc_held"], r["shares"], r["debt"], r["pref"], r["cash"], r.get("mnav_ev"), r["missing"])
