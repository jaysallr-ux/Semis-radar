import json, os, datetime as dt, statistics as st
B = os.path.join(os.path.dirname(__file__), "..", "data")
OUT = os.path.dirname(__file__)
d = json.load(open(os.path.join(B, "price_models.json")))
def wser(k):
	return {dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat(): y for x, y in zip(d[k]["x"], d[k]["y"]) if y is not None}
P, C, R, DL = wser("market"), wser("cvdd"), wser("realised"), wser("delta")
S = json.load(open(os.path.join(B, "probe", "supply_daily_eod.json")))
coc = json.load(open(os.path.join(B, "probe", "coc_onchainoriginals.json")))
def cser(n):
	return {x[:10]: y for x, y in zip(coc[n]["x"], coc[n]["y"]) if y is not None}
CB, CR, CD = cser("Balanced Price"), cser("Realised Price"), cser("Delta Price")
H = [(x, y) for x, y in zip(coc["Price"]["x"], coc["Price"]["y"]) if y is not None]
HLOW = {}
for x, y in H:
	k = x[:10]
	HLOW[k] = min(HLOW.get(k, y), y)
days = sorted(set(P) & set(C) & set(R))
last_sup = None
rows = []
for day in days:
	s = S.get(day)
	if s is None:
		continue
	tp = C[day] * 6e6 / s
	bal = R[day] - tp
	rows.append({"date": day, "price": P[day], "cvdd": C[day], "cvdd105": C[day] * 1.05, "realized": R[day], "delta": DL.get(day), "transferred": tp, "balanced": bal, "coc_balanced": CB.get(day), "supply": s, "hourly_low": HLOW.get(day)})
idx = {r["date"]: i for i, r in enumerate(rows)}
with open(os.path.join(OUT, "btc_models_daily.csv"), "w") as f:
	cols = ["date", "price", "cvdd", "cvdd105", "realized", "delta", "transferred", "balanced", "coc_balanced", "supply", "hourly_low"]
	f.write(",".join(cols) + "\n")
	for r in rows:
		f.write(",".join("" if r[c] is None else (r[c] if c == "date" else f"{r[c]:.4f}") for c in cols) + "\n")
res = {"meta": {"price": "woocharts market (Glassnode daily, UTC)", "cvdd_realized_delta": "woocharts", "supply": "blockchain.info total-bitcoins, last block of UTC day", "balanced": "realized - cvdd*6e6/supply", "coc": "charts-cdn.checkonchain.com pricing_onchainoriginals", "hourly_low": "checkonchain hourly price, min of UTC day (not true intraday low)", "first_date": rows[0]["date"], "last_date": rows[-1]["date"]}}
def pct(a, b):
	return (a / b - 1) * 100
xs = [(r["date"], pct(r["balanced"], r["coc_balanced"])) for r in rows if r["coc_balanced"] and r["coc_balanced"] > 0]
def summ(lst):
	v = [abs(e) for _, e in lst]
	return {"n": len(v), "median_abs_pct": round(st.median(v), 3), "p95_abs_pct": round(sorted(v)[int(len(v) * 0.95)], 3), "max_abs_pct": round(max(v), 3)}
res["balanced_vs_coc"] = {"all": summ(xs), "since_2015": summ([e for e in xs if e[0] >= "2015-01-01"]), "since_2020": summ([e for e in xs if e[0] >= "2020-01-01"]), "latest": {"date": xs[-1][0], "derived": round(rows[idx[xs[-1][0]]]["balanced"], 2), "coc": round(rows[idx[xs[-1][0]]]["coc_balanced"], 2), "diff_pct": round(xs[-1][1], 3)}}
rx = [(day, pct(R[day], CR[day])) for day in days if day in CR and CR[day] > 0]
res["realized_woo_vs_coc"] = {"since_2015": summ([e for e in rx if e[0] >= "2015-01-01"]), "latest": rx[-1]}
win_top = [("2011-01-01", "2011-08-31"), ("2013-10-01", "2014-01-31"), ("2017-11-01", "2018-01-31"), ("2019-05-01", "2019-08-31"), ("2021-10-01", "2021-12-31"), ("2025-01-01", "2025-12-31")]
def argext(a, b, key, fn):
	c = [r for r in rows if a <= r["date"] <= b]
	return fn(c, key=lambda r: r[key])
tops = [argext(a, b, "price", max)["date"] for a, b in win_top]
cycles = []
for i, t in enumerate(tops):
	end = tops[i + 1] if i + 1 < len(tops) else rows[-1]["date"]
	if i + 1 < len(tops) and tops[i + 1] < t:
		end = rows[-1]["date"]
	seg = [r for r in rows if t <= r["date"] <= end]
	b = min(seg, key=lambda r: r["price"])
	cycles.append({"top": t, "top_price": rows[idx[t]]["price"], "bottom": b["date"], "end": end, "ongoing": i == len(tops) - 1})
order = sorted(cycles, key=lambda c: c["top"])
for i, c in enumerate(order):
	nxt = order[i + 1]["top"] if i + 1 < len(order) else rows[-1]["date"]
	seg = [r for r in rows if c["top"] <= r["date"] < nxt] if i + 1 < len(order) else [r for r in rows if r["date"] >= c["top"]]
	b = min(seg, key=lambda r: r["price"])
	c["bottom"], c["end"], c["ongoing"] = b["date"], nxt, i + 1 == len(order)
res["cycles"] = order
bt = []
for c in order:
	r = rows[idx[c["bottom"]]]
	near = [rows[j] for j in range(max(0, idx[c["bottom"]] - 3), min(len(rows), idx[c["bottom"]] + 4)) if rows[j]["hourly_low"]]
	hl = min(near, key=lambda q: q["hourly_low"]) if near else None
	g = {"cycle_top": c["top"], "bottom_date": c["bottom"], "ongoing": c["ongoing"], "close": round(r["price"], 2), "hourly_low_pm3d": round(hl["hourly_low"], 2) if hl else None, "hourly_low_date": hl["date"] if hl else None}
	for k in ["cvdd", "balanced", "coc_balanced", "realized", "delta"]:
		v = r[k]
		g[k] = round(v, 2) if v else None
		g[k + "_ratio"] = round(r["price"] / v, 4) if v and v > 0 else None
	g["cvdd_ratio_hourly_low"] = round(hl["hourly_low"] / r["cvdd"], 4) if hl else None
	bt.append(g)
res["bottom_gaps"] = bt
mults = [1.30, 1.25, 1.20, 1.15, 1.10, 1.05, 1.00, 0.95]
bk = []
for c in order:
	seg = [r for r in rows if c["top"] <= r["date"] < c["end"]] if not c["ongoing"] else [r for r in rows if r["date"] >= c["top"]]
	for m in mults:
		e = next((r for r in seg if r["price"] <= m * r["cvdd"]), None)
		item = {"cycle_top": c["top"], "mult": m, "ongoing": c["ongoing"]}
		if not e:
			item["triggered"] = False
			bk.append(item)
			continue
		after = [r for r in seg if r["date"] >= e["date"]]
		lo = min(after, key=lambda r: r["price"])
		hls = [r for r in after if r["hourly_low"]]
		hlo = min(hls, key=lambda r: r["hourly_low"]) if hls else None
		be = next((r for r in rows[idx[e["date"]] + 1:] if r["date"] > lo["date"] and r["price"] >= e["price"]), None)
		item.update({"triggered": True, "entry_date": e["date"], "entry_close": round(e["price"], 2), "entry_cvdd": round(e["cvdd"], 2), "min_close_date": lo["date"], "min_close": round(lo["price"], 2), "max_dd_close_pct": round(pct(lo["price"], e["price"]), 2), "max_dd_hourly_pct": round(pct(hlo["hourly_low"], e["price"]), 2) if hlo else None, "days_to_low": (dt.date.fromisoformat(lo["date"]) - dt.date.fromisoformat(e["date"])).days, "breakeven_date": be["date"] if be else None, "days_underwater": (dt.date.fromisoformat(be["date"]) - dt.date.fromisoformat(e["date"])).days if be else None})
		bk.append(item)
res["backtest"] = bk
cur = rows[-1]
res["current"] = {"date": cur["date"], "price": round(cur["price"], 2), "cvdd": round(cur["cvdd"], 2), "cvdd105": round(cur["cvdd105"], 2), "ratio": round(cur["price"] / cur["cvdd"], 4), "balanced": round(cur["balanced"], 2), "coc_balanced": cur["coc_balanced"], "supply": cur["supply"]}
json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1)
print(json.dumps({k: res[k] for k in ["balanced_vs_coc", "realized_woo_vs_coc", "current"]}, indent=1))
print("cycles", [(c["top"], c["bottom"]) for c in order])
for g in bt:
	print(g)
print()
for b in bk:
	if b.get("triggered"):
		print(b["cycle_top"], b["mult"], b["entry_date"], b["entry_close"], "->", b["min_close_date"], b["max_dd_close_pct"], b["max_dd_hourly_pct"], "d", b["days_to_low"], "be", b["breakeven_date"], b["days_underwater"])
	else:
		print(b["cycle_top"], b["mult"], "미발동")
lim = []
for c in order:
	seg = [r for r in rows if c["top"] <= r["date"] < c["end"]] if not c["ongoing"] else [r for r in rows if r["date"] >= c["top"]]
	for m in mults:
		item = {"cycle_top": c["top"], "mult": m, "ongoing": c["ongoing"]}
		e = next((r for r in seg if r["hourly_low"] and r["hourly_low"] <= m * r["cvdd"]), None)
		if not e:
			item["triggered"] = False
			lim.append(item)
			continue
		fill = m * e["cvdd"]
		after = [r for r in seg if r["date"] >= e["date"] and r["hourly_low"]]
		lo = min(after, key=lambda r: r["hourly_low"])
		lc = min([r for r in seg if r["date"] >= e["date"]], key=lambda r: r["price"])
		be = next((r for r in rows[idx[e["date"]]:] if r["date"] >= lo["date"] and r["price"] >= fill), None)
		item.update({"triggered": True, "fill_date": e["date"], "fill_price": round(fill, 2), "low_date": lo["date"], "hourly_low": round(lo["hourly_low"], 2), "max_dd_hourly_pct": round(pct(lo["hourly_low"], fill), 2), "max_dd_close_pct": round(pct(lc["price"], fill), 2), "days_to_low": (dt.date.fromisoformat(lo["date"]) - dt.date.fromisoformat(e["date"])).days, "breakeven_close_date": be["date"] if be else None, "days_underwater": (dt.date.fromisoformat(be["date"]) - dt.date.fromisoformat(e["date"])).days if be else None})
		lim.append(item)
res["backtest_limit"] = lim
res["meta"]["backtest_close"] = "entry at first daily close <= mult*CVDD after cycle top; drawdown to lowest later close in cycle"
res["meta"]["backtest_limit"] = "limit fill at mult*CVDD on first day hourly low touches it; drawdown to lowest later hourly low in cycle"
res["meta"]["caveat_hourly"] = "checkonchain hourly series in early years looks forward-filled from daily (2011 hourly low == close), so pre-2014 hourly lows are not reliable intraday lows"
json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1)
print("\nLIMIT")
for b in lim:
	if b.get("triggered"):
		print(b["cycle_top"], b["mult"], b["fill_date"], b["fill_price"], "->", b["low_date"], b["hourly_low"], "hDD", b["max_dd_hourly_pct"], "cDD", b["max_dd_close_pct"], "d", b["days_to_low"], "uw", b["days_underwater"])
	else:
		print(b["cycle_top"], b["mult"], "미발동")
