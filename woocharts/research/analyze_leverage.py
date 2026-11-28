import json, os, datetime as dt
H = os.path.dirname(__file__)
B = os.path.join(H, "..", "data")
d = json.load(open(os.path.join(B, "price_models.json")))
CV = {dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat(): y for x, y in zip(d["cvdd"]["x"], d["cvdd"]["y"]) if y}
prev = json.load(open(os.path.join(H, "results.json")))
cycles = prev["cycles"]
EX = {}
for n in ["bitstamp", "coinbase", "binance"]:
	EX[n] = json.load(open(os.path.join(B, "exchanges", f"{n}_1d.json")))["ohlc"]
CLEAN = []
for n in EX:
	for day, v in EX[n].items():
		oth = [EX[m][day][2] for m in EX if m != n and day in EX[m]]
		if oth and v[2] < 0.7 * min(oth):
			CLEAN.append({"exchange": n, "date": day, "raw_low": v[2], "other_lows": oth, "replaced_with": min(v[0], v[3])})
			EX[n][day] = [v[0], v[1], min(v[0], v[3]), v[3]]
DAYS = sorted(set(CV) | set(EX["bitstamp"]))
SKIP = {"2011-06-08": "bitstamp 2011 data too thin (daily low 2.50 on bottom day vs Mt.Gox-era 2.05), excluded"}
def shift(day, k):
	return (dt.date.fromisoformat(day) - dt.timedelta(days=k)).isoformat()
def worst_low(day):
	v = [EX[n][day][2] for n in EX if day in EX[n]]
	return min(v) if v else None
MULTS = [1.00, 1.05, 1.10, 1.15, 1.20, 1.30]
LAGS = [0, 1, 2]
FEE, MMR, FUND = 0.0005, 0.005, 0.0003
LEVS = [1, 2, 3, 5]
def cyc_days(c):
	return [x for x in DAYS if x >= c["top"] and (c["ongoing"] or x < c["end"])]
def run(c, m, lag, ex, stress):
	o = EX[ex]
	seg = [x for x in cyc_days(c) if x in o and shift(x, lag) in CV]
	for i, x in enumerate(seg):
		th = m * CV[shift(x, lag)]
		op, hi, lo, cl = o[x]
		if lo <= th:
			fill = min(th, op)
			after = seg[i:]
			lows = [(y, (worst_low(y) if stress else o[y][2])) for y in after]
			lows = [(y, v) for y, v in lows if v is not None]
			ly, lv = min(lows, key=lambda t: t[1])
			be = next((y for y in after if y >= ly and o[y][3] >= fill), None)
			path = [(y, (dt.date.fromisoformat(y) - dt.date.fromisoformat(x)).days, v / fill) for y, v in lows]
			lev = {}
			for L in LEVS:
				liq = None
				for y, k, r in path:
					rl = (1 - 1 / L + FEE + FUND * k) / (1 - MMR)
					if r <= rl:
						liq = y
						break
				lev[str(L)] = liq
			worst = min(path, key=lambda t: t[2])
			lmax = 1 / (1 + FEE + FUND * worst[1] - (1 - MMR) * worst[2]) if worst[2] < 1 else None
			return {"fill_date": x, "fill": round(fill, 2), "gap_open_fill": op < th, "low_date": ly, "low": round(lv, 2), "max_dd_pct": round((lv / fill - 1) * 100, 2), "days_to_low": (dt.date.fromisoformat(ly) - dt.date.fromisoformat(x)).days, "breakeven_date": be, "days_underwater": (dt.date.fromisoformat(be) - dt.date.fromisoformat(x)).days if be else None, "liquidated": lev, "max_safe_leverage": round(lmax, 2) if lmax else None}
	return None
out = {"meta": {"entry": "limit at mult*CVDD(t-lag); fill if exchange daily low <= limit; fill=min(limit, open)", "drawdown": "fill -> lowest daily low until next cycle top (ongoing: until now)", "stress": "lows replaced by worst low across bitstamp/coinbase/binance each day, entry still on named exchange", "leverage_model": f"isolated linear long, taker fee {FEE}, maintenance margin {MMR}, funding {FUND}/day of notional paid every day held (conservative, ignores negative funding), liquidation checked on daily low", "caveats": ["exchanges liquidate on mark/index price (multi-exchange average), so single-exchange spot lows are harsher than real liquidation triggers", "derivative last-trade wicks (e.g. BitMEX 2020-03-13) can be deeper than spot; BitMEX data blocked, not included", "bitstamp 2011 data is thin (few trades/day)", "cvdd for day t is computed from day t close, so lag 0 uses information not available intraday"]}, "runs": []}
for c in cycles:
	if c["top"] in SKIP:
		continue
	for ex in EX:
		for m in MULTS:
			for lag in LAGS:
				for stress in (False, True):
					if stress and ex != "bitstamp":
						continue
					r = run(c, m, lag, ex, stress)
					out["runs"].append({"cycle_top": c["top"], "bottom": c["bottom"], "ongoing": c["ongoing"], "exchange": ex + ("+worst" if stress else ""), "mult": m, "lag": lag, "result": r})
ETF_EXP, ETF_FIN = 0.0185, 0.05
etf = []
o = EX["bitstamp"]
for c in cycles:
	if c["top"] in SKIP:
		continue
	seg = [x for x in cyc_days(c) if x in o and shift(x, 1) in CV]
	for m in MULTS:
		e = next((i for i, x in enumerate(seg) if o[x][3] <= m * CV[shift(x, 1)]), None)
		if e is None:
			etf.append({"cycle_top": c["top"], "mult": m, "triggered": False})
			continue
		x0 = seg[e]
		be = next((j for j in range(e + 1, len(seg)) if seg[j] > c["bottom"] and o[seg[j]][3] >= o[x0][3]), len(seg) - 1)
		win = seg[e:be + 1]
		rec = {"cycle_top": c["top"], "mult": m, "triggered": True, "entry_date": x0, "entry_close": o[x0][3], "window_end": win[-1], "window_end_reason": "spot back to entry close" if be < len(seg) - 1 or o[seg[-1]][3] >= o[x0][3] else "cycle end / still below entry"}
		for L in (2, 3):
			nav, pk, mdd, spot_be_nav = 1.0, 1.0, 0.0, None
			worst_nav, worst_day = 1.0, x0
			for a, b in zip(win, win[1:]):
				r = o[b][3] / o[a][3] - 1
				nav *= max(0.0, 1 + L * r - (ETF_EXP + (L - 1) * ETF_FIN) / 365)
				if nav < worst_nav:
					worst_nav, worst_day = nav, b
				spot_be_nav = nav
			lows = [o[y][3] for y in win]
			rec[f"{L}x"] = {"worst_nav_loss_pct": round((worst_nav - 1) * 100, 2), "worst_day": worst_day, "spot_worst_close_pct": round((min(lows) / o[x0][3] - 1) * 100, 2), "naive_L_times_spot_pct": round(L * (min(lows) / o[x0][3] - 1) * 100, 2), "nav_at_window_end": round(spot_be_nav, 4) if spot_be_nav else None}
		etf.append(rec)
out["etf"] = etf
out["data_cleaning"] = CLEAN
out["skipped_cycles"] = SKIP
out["etf_meta"] = f"daily rebalanced, bitstamp UTC close-to-close every calendar day, expense {ETF_EXP}/yr + financing {ETF_FIN}/yr on (L-1); entry at first close <= mult*CVDD(t-1)"
json.dump(out, open(os.path.join(H, "results_leverage.json"), "w"), indent=1)
def show(ex, lag):
	print(f"\n== {ex} lag{lag}")
	for c in cycles:
		if c["top"] in SKIP:
			continue
		line = [c["bottom"][:4] + ("*" if c["ongoing"] else "")]
		for m in MULTS:
			r = next(z["result"] for z in out["runs"] if z["cycle_top"] == c["top"] and z["exchange"] == ex and z["mult"] == m and z["lag"] == lag)
			line.append("  -" if not r else f"{r['max_dd_pct']:6.1f}")
		print(" ".join(f"{s:>7}" for s in line))
for ex in ["bitstamp", "bitstamp+worst", "coinbase", "binance"]:
	for lag in LAGS:
		show(ex, lag)
print("\n== 1.05 lag1 detail")
for z in out["runs"]:
	if z["mult"] == 1.05 and z["lag"] == 1 and z["result"]:
		r = z["result"]
		print(z["bottom"][:4], z["exchange"], r["fill_date"], r["fill"], r["gap_open_fill"], r["low_date"], r["low"], r["max_dd_pct"], r["days_to_low"], r["days_underwater"], r["liquidated"], r["max_safe_leverage"])
print("\n== ETF")
for e in etf:
	if e["triggered"] and e["mult"] in (1.05, 1.1, 1.2):
		print(e["cycle_top"], e["mult"], e["entry_date"], e["window_end"], e["window_end_reason"], "2x", e["2x"]["worst_nav_loss_pct"], e["2x"]["spot_worst_close_pct"], e["2x"]["nav_at_window_end"], "3x", e["3x"]["worst_nav_loss_pct"], e["3x"]["nav_at_window_end"])
print("clean", CLEAN)
