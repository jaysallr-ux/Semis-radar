import json, os, datetime as dt
H = os.path.dirname(__file__)
B = os.path.join(H, "..", "data")
R = json.load(open(os.path.join(H, "mstr_mnav_daily.json")))
D = {r["date"]: r for r in R}
TD = [r["date"] for r in R]
MO = json.load(open(os.path.join(B, "equities", "MSTR_1d.json")))["ohlc"]
d = json.load(open(os.path.join(B, "price_models.json")))
CV = {dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat(): y for x, y in zip(d["cvdd"]["x"], d["cvdd"]["y"]) if y}
BS = json.load(open(os.path.join(B, "exchanges", "bitstamp_1d.json")))["ohlc"]
coc = json.load(open(os.path.join(B, "probe", "coc_onchainoriginals.json")))["Price"]
HR = [(x, y) for x, y in zip(coc["x"], coc["y"]) if y]
prev = lambda day: (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
def is_edt(dd):
	y = dd.year
	m3 = dt.date(y, 3, 8) + dt.timedelta(days=(6 - dt.date(y, 3, 8).weekday()) % 7)
	n1 = dt.date(y, 11, 1) + dt.timedelta(days=(6 - dt.date(y, 11, 1).weekday()) % 7)
	return m3 <= dd < n1
CYC = {"2022": ("2021-11-08", "2023-06-30", False), "2025-26": ("2025-10-06", TD[-1], True)}
def signal(top, end, m):
	for x in sorted(BS):
		if top <= x <= end and x in BS and prev(x) in CV and BS[x][2] <= m * CV[prev(x)]:
			th = m * CV[prev(x)]
			hit = next((h for h, y in HR if h[:10] == x and y <= th), None)
			ch = 20 if is_edt(dt.date.fromisoformat(x)) else 21
			if x in D and hit and int(hit[11:13]) < ch:
				return x, x, hit
			return x, next(t for t in TD if t > x), hit
	return None, None, None
def mnav_prev(t):
	i = TD.index(t)
	return D[TD[i - 1]].get("mnav_ev") if i > 0 else None
def sim(entry, end, plan, remainder_days=None, budget=1.0):
	days = [t for t in TD if entry <= t <= end]
	units, cost, buys = 0.0, 0.0, []
	ref = MO[entry][3]
	pending = [dict(o) for o in plan]
	cash = budget
	worst_dep, worst_bud = 0.0, 0.0
	dl = (dt.date.fromisoformat(entry) + dt.timedelta(days=remainder_days)).isoformat() if remainder_days else None
	for t in days:
		o, h, l, c = MO[t][:4]
		for od in list(pending):
			amt = od["w"] * budget
			px = None
			if od["type"] == "close" and t == entry:
				px = c
			elif od["type"] == "limit" and t > entry:
				lv = ref * (1 + od["drop"])
				if l <= lv:
					px = min(lv, o)
			elif od["type"] == "mnav_close" and t > entry and od.get("from", entry) <= t:
				mp = mnav_prev(t)
				if mp is not None and mp <= od["cap"]:
					px = c
			elif od["type"] == "date_close" and t >= od["date"]:
				px = c
			if px:
				units += amt / px
				cost += amt
				cash -= amt
				buys.append({"date": t, "w": od["w"], "px": round(px, 3), "mnav_prev": round(mnav_prev(t), 3) if mnav_prev(t) else None, "why": od["type"]})
				pending.remove(od)
		if dl and t >= dl and pending:
			rest = sum(od["w"] for od in pending)
			units += rest * budget / c
			cost += rest * budget
			cash -= rest * budget
			buys.append({"date": t, "w": round(rest, 4), "px": round(c, 3), "mnav_prev": round(mnav_prev(t), 3) if mnav_prev(t) else None, "why": f"remainder_{remainder_days}d"})
			pending = []
		if cost > 0:
			u_prev = sum(b["w"] * budget / b["px"] for b in buys if b["date"] < t)
			u_lim = sum(b["w"] * budget / b["px"] for b in buys if b["date"] == t and b["why"] == "limit")
			u_cls = sum(b["w"] * budget / b["px"] for b in buys if b["date"] == t and b["why"] != "limit")
			mark = (u_prev + u_lim) * l + u_cls * c
			worst_dep = min(worst_dep, mark / cost - 1)
			worst_bud = min(worst_bud, (mark - cost) / budget)
	lows = [MO[t][2] for t in days]
	cyc_low = min(lows)
	rets = {}
	for hd in (180, 365):
		hh = (dt.date.fromisoformat(entry) + dt.timedelta(days=hd)).isoformat()
		hz = next((t for t in TD if t >= hh), None)
		if hz:
			u2 = sum(b["w"] * budget / b["px"] for b in buys if b["date"] <= hz)
			c2 = sum(b["w"] * budget for b in buys if b["date"] <= hz)
			rets[hd] = (u2 * MO[hz][3] + (budget - c2)) / budget - 1
	val = rets.get(365)
	avg = cost / units if units else None
	return {"filled_pct": round(cost / budget * 100), "avg": round(avg, 2) if avg else None, "avg_vs_low_pct": round((avg / cyc_low - 1) * 100, 1) if avg else None, "worst_loss_on_deployed_pct": round(worst_dep * 100, 1), "worst_loss_vs_budget_pct": round(worst_bud * 100, 1), "ret_180d_on_budget_pct": round(rets[180] * 100, 1) if 180 in rets else None, "ret_365d_on_budget_pct": round(val * 100, 1) if val is not None else None, "n_buys": len(buys), "avg_mnav_prev_at_buys": round(sum(b["mnav_prev"] * b["w"] for b in buys if b["mnav_prev"]) / max(1e-9, sum(b["w"] for b in buys if b["mnav_prev"])), 3) if buys else None, "buys": buys}
DROPS = [-0.05, -0.10, -0.15, -0.20, -0.25, -0.30]
PLANS = {
	"즉시 100%": [{"type": "close", "w": 1.0}],
	"7등분 (신호일 + -5~-30%)": [{"type": "close", "w": 1 / 7}] + [{"type": "limit", "drop": x, "w": 1 / 7} for x in DROPS],
	"30% + -10/-20/-30%": [{"type": "close", "w": 0.3}] + [{"type": "limit", "drop": x, "w": 0.7 / 3} for x in [-0.10, -0.20, -0.30]],
	"50% + -10/-20/-30%": [{"type": "close", "w": 0.5}] + [{"type": "limit", "drop": x, "w": 0.5 / 3} for x in [-0.10, -0.20, -0.30]],
	"30% + -5~-30% 6등분": [{"type": "close", "w": 0.3}] + [{"type": "limit", "drop": x, "w": 0.7 / 6} for x in DROPS],
}
def time_plan(entry, weeks):
	st = dt.date.fromisoformat(entry)
	return [{"type": "close", "w": 1 / weeks}] + [{"type": "date_close", "date": (st + dt.timedelta(weeks=k)).isoformat(), "w": 1 / weeks} for k in range(1, weeks)]
out = {"meta": {"entry": "signal = Bitstamp daily low <= mult*CVDD(t-1); first buy at MSTR close if first hourly BTC price below threshold was before US close, else next trading day close", "limit_fill": "limit at first-buy price*(1+drop); filled if MSTR daily low <= level; fill = min(level, open)", "mnav_rule": "mNAV used only as reported auxiliary (prev-day point-in-time EV mNAV at each fill); in mNAV-conditional comparison plan, decision uses t-1 mNAV and fills at t close", "loss_metrics": "shares bought at a close are marked at that close on the buy day (not that day's low); limit fills and older shares are marked at the day low. worst_loss_on_deployed = min over days of (units*day_low/cost_so_far - 1); worst_loss_vs_budget = min of (units*day_low - cost_so_far)/budget; unspent cash earns 0", "ret_365d": "(value of shares bought by +180d/+365d at that close + unspent cash)/budget - 1; uses data beyond the drawdown window", "window": "until 2023-06-30 for 2022 cycle; until latest data for 2025-26 (ongoing)", "caveats": ["2022 is the only completed cycle with MSTR as a BTC treasury company; 2025-26 signals are days old", "multiple multipliers in one cycle are not independent samples", "ladder levels and weights are not optimized; picking the best row is overfitting", "basic shares, not fully diluted"]}, "signals": {}, "results": {}}
for cy, (top, end, ongoing) in CYC.items():
	for m in [1.05, 1.10, 1.15, 1.20]:
		sig, entry, hit = signal(top, end, m)
		key = f"{cy} ×{m:.2f}"
		out["signals"][key] = {"btc_signal": sig, "mstr_first_buy": entry, "first_hour": hit, "mstr_close": MO[entry][3] if entry else None, "mnav_prev": mnav_prev(entry) if entry else None, "ongoing": ongoing}
		if not entry:
			continue
		res = {}
		for pn, plan in PLANS.items():
			for rd in [None, 30, 60, 90]:
				if pn == "즉시 100%" and rd:
					continue
				res[f"{pn}" + (f" / 잔량 {rd}일" if rd else " / 잔량 현금")] = sim(entry, end, plan, rd)
		res["2주 시간분할"] = sim(entry, end, time_plan(entry, 2))
		res["8주 시간분할"] = sim(entry, end, time_plan(entry, 8))
		res["mNAV(전일)≤1.0 조건 / 잔량 90일"] = sim(entry, end, [{"type": "close", "w": 0.0001}] + [{"type": "mnav_close", "cap": 1.0, "w": 0.9999}], 90)
		out["results"][key] = res
json.dump(out, open(os.path.join(H, "results_price_ladder.json"), "w"), indent=1)
for k, v in out["signals"].items():
	print(k, {x: v[x] for x in ["btc_signal", "mstr_first_buy", "mstr_close", "mnav_prev"]})
for k, res in out["results"].items():
	print("\n==", k)
	for n, r in res.items():
		print(f"  {n:34s} fill {r['filled_pct']:3d}% avg {r['avg']} vsLow {r['avg_vs_low_pct']}% worstDep {r['worst_loss_on_deployed_pct']}% worstBud {r['worst_loss_vs_budget_pct']}% 180d {r['ret_180d_on_budget_pct']}% 365d {r['ret_365d_on_budget_pct']}% mNAV {r['avg_mnav_prev_at_buys']}")
