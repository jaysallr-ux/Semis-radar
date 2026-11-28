import json, os, datetime as dt
H = os.path.dirname(__file__)
B = os.path.join(H, "..", "data")
d = json.load(open(os.path.join(B, "price_models.json")))
CV = {dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat(): y for x, y in zip(d["cvdd"]["x"], d["cvdd"]["y"]) if y}
EX = {n: json.load(open(os.path.join(B, "exchanges", f"{n}_1d.json")))["ohlc"] for n in ["bitstamp", "coinbase", "binance"]}
EX["coinbase"]["2017-04-15"] = [1173.13, 1190.99, 1173.13, 1178.85]
MSTR = json.load(open(os.path.join(B, "equities", "MSTR_1d.json")))["ohlc"]
coc = json.load(open(os.path.join(B, "probe", "coc_onchainoriginals.json")))["Price"]
HR = [(x, y) for x, y in zip(coc["x"], coc["y"]) if y]
cyc = {c["top"]: c for c in json.load(open(os.path.join(H, "results.json")))["cycles"]}
CYC = [("2015", "2013-12-04"), ("2018", "2017-12-16"), ("2020", "2019-06-26"), ("2022", "2021-11-08")]
prev = lambda day: (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
LAD = {"1.00 몰빵": [(1.00, 1.0)], "50/50": [(1.05, .5), (1.00, .5)], "GPT 25/45/20/10": [(1.05, .25), (1.00, .45), (0.95, .20), (0.90, .10)], "1.05 몰빵": [(1.05, 1.0)]}
res = {"same_exchange": [], "mstr_timing": {}}
for ex in ["bitstamp", "coinbase", "binance"]:
	o = EX[ex]
	for lab, top in CYC:
		c = cyc[top]
		seg = [x for x in sorted(o) if top <= x < c["end"]]
		if not seg or seg[0] > c["bottom"]:
			continue
		blo = min(seg, key=lambda x: o[x][2])
		bl = o[blo][2]
		touch = {}
		for m in [1.05, 1.00, 0.95, 0.90]:
			t = next((x for x in seg if o[x][2] <= m * CV[prev(x)]), None)
			touch[m] = (t, min(m * CV[prev(t)], o[t][0])) if t else None
		for name, lad in LAD.items():
			f = [(touch[m][1], w) for m, w in lad if touch[m]]
			first = next(touch[m][0] for m, _ in lad if touch[m]) if f else None
			wf = sum(w for _, w in f)
			d30 = (dt.date.fromisoformat(first) + dt.timedelta(days=30)).isoformat() if first else None
			cl = o[d30][3] if d30 in o else None
			avg = 1 / (sum(w / p for p, w in f) + (1 - wf) / cl) if f and cl else None
			res["same_exchange"].append({"exchange": ex, "cycle": lab, "ladder": name, "filled_pct": round(wf * 100), "avg_vs_bottom_pct": round((avg / bl - 1) * 100, 2) if avg else None})
print("exchange  cycle  " + "  ".join(LAD))
for ex in ["bitstamp", "coinbase", "binance"]:
	for lab, _ in CYC:
		r = [z for z in res["same_exchange"] if z["exchange"] == ex and z["cycle"] == lab]
		if r:
			print(ex, lab, [(z["ladder"], z["filled_pct"], z["avg_vs_bottom_pct"]) for z in r])
bs = [z for z in res["same_exchange"] if z["exchange"] == "bitstamp"]
res["bitstamp_avg"] = {n: round(sum(z["avg_vs_bottom_pct"] for z in bs if z["ladder"] == n) / 4, 2) for n in LAD}
print("bitstamp 4-cycle avg", res["bitstamp_avg"])
o = EX["bitstamp"]
th = 1.05 * CV[prev("2022-11-09")]
hit = next((x for x, y in HR if x[:10] == "2022-11-09" and y <= th), None)
mlo = min([k for k in MSTR if "2022-11-01" <= k <= "2023-03-31"], key=lambda k: MSTR[k][2])
nxt = next(k for k in sorted(MSTR) if k > "2022-11-09")
res["mstr_timing"] = {"btc_105_threshold": round(th, 2), "first_hour_coc_price_below": hit, "us_close_utc_nov": "21:00", "mstr_low": [mlo, MSTR[mlo][2]], "same_day_close": MSTR["2022-11-09"][3], "next_day_open": MSTR[nxt][0], "next_day_close": MSTR[nxt][3], "next_day": nxt}
for k in ["same_day_close", "next_day_open", "next_day_close"]:
	res["mstr_timing"][k + "_vs_low_pct"] = round((res["mstr_timing"][k] / MSTR[mlo][2] - 1) * 100, 2)
print(json.dumps(res["mstr_timing"], indent=1))
json.dump(res, open(os.path.join(H, "results_ladder_fix.json"), "w"), indent=1)
