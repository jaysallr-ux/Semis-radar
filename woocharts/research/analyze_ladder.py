import json, os, datetime as dt
H = os.path.dirname(__file__)
B = os.path.join(H, "..", "data")
d = json.load(open(os.path.join(B, "price_models.json")))
CV = {dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat(): y for x, y in zip(d["cvdd"]["x"], d["cvdd"]["y"]) if y}
EX = {n: json.load(open(os.path.join(B, "exchanges", f"{n}_1d.json")))["ohlc"] for n in ["bitstamp", "coinbase", "binance"]}
EX["coinbase"]["2017-04-15"] = [1173.13, 1190.99, 1173.13, 1178.85]
MSTR = json.load(open(os.path.join(B, "equities", "MSTR_1d.json")))["ohlc"]
cyc = {c["top"]: c for c in json.load(open(os.path.join(H, "results.json")))["cycles"]}
CYC = [("2015", "2013-12-04"), ("2018", "2017-12-16"), ("2020", "2019-06-26"), ("2022", "2021-11-08")]
def prev(day, k=1):
	return (dt.date.fromisoformat(day) - dt.timedelta(days=k)).isoformat()
def wlow(day):
	v = [EX[n][day][2] for n in EX if day in EX[n]]
	return min(v)
LADDERS = {"GPT 25/45/20/10": [(1.05, .25), (1.00, .45), (0.95, .20), (0.90, .10)], "1.05 몰빵": [(1.05, 1.0)], "1.00 몰빵": [(1.00, 1.0)], "50/50 (1.05/1.00)": [(1.05, .5), (1.00, .5)], "40/40/20 (1.05/1.00/0.95)": [(1.05, .4), (1.00, .4), (0.95, .2)]}
out = {"btc": [], "touch": [], "mstr_2022": {}}
for lab, top in CYC:
	c = cyc[top]
	seg = [x for x in sorted(EX["bitstamp"]) if top <= x < c["end"]]
	blo = min(seg, key=wlow)
	bottom_low = wlow(blo)
	touch = {}
	for m in [1.05, 1.00, 0.95, 0.90, 0.85]:
		t = next((x for x in seg if wlow(x) <= m * CV[prev(x)]), None)
		touch[str(m)] = None if t is None else {"date": t, "weekday": dt.date.fromisoformat(t).strftime("%a"), "fill": round(min(m * CV[prev(t)], EX["bitstamp"][t][0]), 2)}
	out["touch"].append({"cycle": lab, "bottom_day": blo, "bottom_low": bottom_low, "bottom_over_cvdd": round(bottom_low / CV[prev(blo)], 4), "touch": touch})
	for name, lad in LADDERS.items():
		filled = [(touch[str(m)]["fill"], w) for m, w in lad if touch.get(str(m))]
		wf = sum(w for _, w in filled)
		avg = sum(w for _, w in filled) / sum(w / p for p, w in filled) if filled else None
		day30 = (dt.date.fromisoformat(touch[str(lad[0][0])]["date"]) + dt.timedelta(days=30)).isoformat() if touch.get(str(lad[0][0])) else None
		cl30 = EX["bitstamp"][day30][3] if day30 in EX["bitstamp"] else None
		allb = (wf + (1 - wf)) / (sum(w / p for p, w in filled) + (1 - wf) / cl30) if filled and cl30 else None
		out["btc"].append({"cycle": lab, "ladder": name, "deployed_pct": round(wf * 100), "avg_cost_filled": round(avg, 2) if avg else None, "avg_vs_bottom_pct": round((avg / bottom_low - 1) * 100, 2) if avg else None, "unfilled_bought_d30_avg": round(allb, 2) if allb else None, "d30_vs_bottom_pct": round((allb / bottom_low - 1) * 100, 2) if allb else None})
t22 = next(t for t in out["touch"] if t["cycle"] == "2022")["touch"]
def mstr_on(day):
	x = next(k for k in sorted(MSTR) if k >= day)
	return x, MSTR[x][3]
mlo = min([k for k in MSTR if "2022-11-01" <= k <= "2023-03-31"], key=lambda k: MSTR[k][2])
m22 = {"mstr_low": [mlo, MSTR[mlo][2]], "tranches": {}}
for m in ["1.05", "1.0", "0.95", "0.9"]:
	if t22.get(m):
		day, p = mstr_on(t22[m]["date"])
		m22["tranches"][m] = {"btc_touch": t22[m]["date"], "mstr_day": day, "mstr_close": round(p, 2), "vs_mstr_low_pct": round((p / MSTR[mlo][2] - 1) * 100, 2)}
lad = LADDERS["GPT 25/45/20/10"]
fl = [(m22["tranches"][("1.0" if m == 1.0 else str(m))]["mstr_close"], w) for m, w in lad if ("1.0" if m == 1.0 else str(m)) in m22["tranches"]]
m22["gpt_ladder_deployed_pct"] = round(sum(w for _, w in fl) * 100)
m22["gpt_ladder_avg"] = round(sum(w for _, w in fl) / sum(w / p for p, w in fl), 2)
for days in [30, 45, 60]:
	x = (dt.date.fromisoformat(t22["1.05"]["date"]) + dt.timedelta(days=days)).isoformat()
	dd, p = mstr_on(x)
	m22[f"mstr_close_d{days}_after_btc105"] = [dd, round(p, 2), round((p / MSTR[mlo][2] - 1) * 100, 2)]
out["mstr_2022"] = m22
json.dump(out, open(os.path.join(H, "results_ladder.json"), "w"), indent=1)
for t in out["touch"]:
	print(t["cycle"], "bottom", t["bottom_day"], t["bottom_low"], "x", t["bottom_over_cvdd"], {k: (v["date"], v["weekday"]) if v else None for k, v in t["touch"].items()})
print()
for r in out["btc"]:
	print(r)
print(json.dumps(m22, indent=1))
