import json, os, datetime as dt
H = os.path.dirname(__file__)
E = os.path.join(H, "..", "data", "equities")
X = os.path.join(H, "..", "data", "exchanges")
def load(sym):
	return json.load(open(os.path.join(E, f"{sym}_1d.json")))["ohlc"]
MSTR, MSTU, MSTX, ASST = load("MSTR"), load("MSTU"), load("MSTX"), load("ASST")
BS = json.load(open(os.path.join(X, "bitstamp_1d.json")))["ohlc"]
kpi = json.load(open(os.path.join(E, "strategy_api_kpi.json")))["results"]
mk = json.load(open(os.path.join(E, "strategy_api_purchases.json")))[0]
num = lambda s: float(str(s).replace(",", ""))
BTC_NOW = kpi["latestPrice"]
co = {
	"MSTR": {"btc": num(kpi["btcHoldings"]), "debt": num(mk["debt"]) * 1e6, "pref": num(mk["pref"]) * 1e6, "cash": num(mk["usdAssets"]) * 1e6, "shares": num(mk["marketCap"]) * 1e6 / mk["ufPrice"], "price": mk["ufPrice"], "div_yr": kpi["totalAnnualDividends"], "src": "api.strategy.com KPI " + kpi["msTimestamp"].__str__()},
	"ASST": {"btc": 43404, "debt": 0.0, "pref": 14030176 * 100 + 7141037 * 100, "cash": 462.4e6 + 47.268e6, "shares": 130044690, "price": None, "div_yr": None, "src": "Strive 8-K 2026-11-23 (as of 2026-11-20); pref at $100 stated per share (SATA per 10-Q: $1.3B for 12.94M sh); SATS stated amount assumed $100"},
}
last_asst = sorted(ASST)[-1]
co["ASST"]["price"] = ASST[last_asst][3]
co["ASST"]["div_yr"] = 14030176 * 100 * 0.125 + 7141037 * 100 * 0.15
co["ASST"]["div_note"] = "estimate: SATA variable rate assumed 12.5%, SATS 15%"
for k, c in co.items():
	nav = c["btc"] * BTC_NOW
	mcap = c["price"] * c["shares"]
	ev = mcap + c["debt"] + c["pref"] - c["cash"]
	c.update({"btc_nav": nav, "mcap": mcap, "ev": ev, "mnav": ev / nav, "lev_ratio": (c["debt"] + c["pref"]) / nav, "btc_per_share": c["btc"] / c["shares"], "zero_equity_btc_with_cash": (c["debt"] + c["pref"] - c["cash"]) / c["btc"], "zero_equity_btc_no_cash": (c["debt"] + c["pref"]) / c["btc"], "cash_years_of_div": c["cash"] / c["div_yr"]})
def px(c, btc, m, years=0.0):
	cash = c["cash"] - c["div_yr"] * years
	return (m * c["btc"] * btc - c["debt"] - c["pref"] + cash) / c["shares"]
scen = {"entry_btc": [40000, 50000], "exit_btc": [100000, 150000, 200000], "entry_m": [0.8, 1.0], "exit_m": [1.0, 1.5, 2.0], "years": 2}
grid = []
for k, c in co.items():
	for eb in scen["entry_btc"]:
		for em in scen["entry_m"]:
			p0 = px(c, eb, em)
			for xb in scen["exit_btc"]:
				for xm in scen["exit_m"]:
					p1 = px(c, xb, xm, scen["years"])
					grid.append({"co": k, "entry_btc": eb, "entry_mnav": em, "entry_px": round(p0, 2), "exit_btc": xb, "exit_mnav": xm, "exit_px": round(p1, 2), "ret_x": round(p1 / p0, 2) if p0 > 0 else None, "btc_ret_x": round(xb / eb, 2)})
def trade_days(o):
	return sorted(o)
def first_on_after(o, day):
	return next((d for d in trade_days(o) if d >= day), None)
hist = {}
d0 = first_on_after(MSTR, "2022-11-09")
seg = [d for d in trade_days(MSTR) if d0 <= d <= "2023-12-31"]
lo = min(seg, key=lambda d: MSTR[d][2])
be = next((d for d in seg if d > lo and MSTR[d][3] >= MSTR[d0][3]), None)
hist["mstr_after_btc_105_2022"] = {"btc_trigger": "2022-11-09", "mstr_entry_day": d0, "mstr_entry_close": MSTR[d0][3], "mstr_low_day": lo, "mstr_low": MSTR[lo][2], "max_dd_pct": round((MSTR[lo][2] / MSTR[d0][3] - 1) * 100, 2), "days_to_low": (dt.date.fromisoformat(lo) - dt.date.fromisoformat(d0)).days, "back_to_entry": be, "btc_low_day": "2022-11-21"}
def peak(o, a, b):
	s = [d for d in o if a <= d <= b]
	return max(s, key=lambda d: o[d][1])
def trough(o, a, b):
	s = [d for d in o if a <= d <= b]
	return min(s, key=lambda d: o[d][2])
mt, mp = trough(MSTR, "2022-11-01", "2023-01-31"), peak(MSTR, "2024-10-01", "2025-01-31")
bt, bp = trough(BS, "2022-11-01", "2022-12-31"), peak(BS, "2024-10-01", "2025-12-31")
hist["upcycle_2022_2024"] = {"mstr_low": [mt, MSTR[mt][2]], "mstr_high": [mp, MSTR[mp][1]], "mstr_x": round(MSTR[mp][1] / MSTR[mt][2], 2), "btc_low": [bt, BS[bt][2]], "btc_high": [bp, BS[bp][1]], "btc_x": round(BS[bp][1] / BS[bt][2], 2)}
for name, a, b in [("btc_105_entry_to_mstr_peak", d0, None)]:
	pk = peak(MSTR, d0, "2025-01-31")
	hist["entry_105_to_mstr_peak"] = {"mstr_x": round(MSTR[pk][1] / MSTR[d0][3], 2), "peak_day": pk}
def letf(o, L, a, b, exp, fin=0.05):
	days = [d for d in trade_days(o) if a <= d <= b]
	nav, worst, wd = 1.0, 1.0, a
	path = {}
	for p, q in zip(days, days[1:]):
		gap = (dt.date.fromisoformat(q) - dt.date.fromisoformat(p)).days
		r = o[q][3] / o[p][3] - 1
		nav *= max(0.0, 1 + L * r - (exp + (L - 1) * fin) * gap / 365)
		path[q] = nav
		if nav < worst:
			worst, wd = nav, q
	return nav, worst, wd, path
syn = {}
for a, b, tag in [(d0, be or "2023-12-31", "entry_105_to_mstr_breakeven"), (d0, "2024-11-21", "entry_105_to_2024_11_21")]:
	nav, worst, wd, _ = letf(MSTR, 2, a, b, 0.0105)
	syn[tag] = {"start": a, "end": b, "mstr_x": round(MSTR[b if b in MSTR else first_on_after(MSTR, b)][3] / MSTR[a][3], 2), "syn2x_x": round(nav, 2), "syn2x_worst_pct": round((worst - 1) * 100, 2), "worst_day": wd}
hist["synthetic_2x_mstr"] = syn
cur = {}
top = "2025-10-06"
for sym, o in [("MSTR", MSTR), ("MSTU", MSTU), ("MSTX", MSTX), ("ASST", ASST)]:
	a = first_on_after(o, top)
	z = sorted(o)[-1]
	lo2 = min([d for d in o if d >= a], key=lambda d: o[d][2])
	cur[sym] = {"from": a, "start_close": o[a][3], "last": z, "last_close": o[z][3], "chg_pct": round((o[z][3] / o[a][3] - 1) * 100, 2), "low_day": lo2, "low": o[lo2][2], "max_dd_pct": round((o[lo2][2] / o[a][3] - 1) * 100, 2)}
a = first_on_after(BS, top)
cur["BTC_bitstamp"] = {"from": a, "start_close": BS[a][3], "last": sorted(BS)[-1], "last_close": BS[sorted(BS)[-1]][3], "chg_pct": round((BS[sorted(BS)[-1]][3] / BS[a][3] - 1) * 100, 2)}
nav, worst, wd, _ = letf(MSTR, 2, first_on_after(MSTR, top), sorted(MSTR)[-1], 0.0105)
cur["synthetic_2x_mstr_same_window"] = {"chg_pct": round((nav - 1) * 100, 2)}
hist["current_cycle_from_top"] = cur
out = {"companies": co, "scenario_meta": scen | {"model": "price = (mNAV*BTC*btc_price - debt - pref + cash)/fully_diluted_shares; mNAV=EV/BTC NAV (Strategy definition); static capital structure (no new issuance or BTC buys); exit cash reduced by years*annual dividends"}, "grid": grid, "history": hist}
json.dump(out, open(os.path.join(H, "results_treasury.json"), "w"), indent=1, default=str)
for k, c in co.items():
	print(k, {x: (round(v, 4) if isinstance(v, float) else v) for x, v in c.items() if x != "src"})
print(json.dumps(hist, indent=1))
for g in grid:
	if g["entry_mnav"] == 1.0 or g["co"] == "ASST":
		print(g)
