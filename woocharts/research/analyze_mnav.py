import json, os, datetime as dt
H = os.path.dirname(__file__)
B = os.path.join(H, "..", "data")
R = [r for r in json.load(open(os.path.join(H, "mstr_mnav_daily.json")))]
D = {r["date"]: r for r in R}
TD = [r["date"] for r in R]
d = json.load(open(os.path.join(B, "price_models.json")))
CV = {dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat(): y for x, y in zip(d["cvdd"]["x"], d["cvdd"]["y"]) if y}
BS = json.load(open(os.path.join(B, "exchanges", "bitstamp_1d.json")))["ohlc"]
coc = json.load(open(os.path.join(B, "probe", "coc_onchainoriginals.json")))["Price"]
HR = [(x, y) for x, y in zip(coc["x"], coc["y"]) if y]
prev = lambda day: (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
ok = lambda r: r.get("mnav_ev") is not None and not r["missing"]
CYC = {"2022": ("2021-11-08", "2023-06-30"), "2025-26": ("2025-10-06", TD[-1])}
out = {"cycles": {}, "strategies": {}, "mnav_down_btc_up": {}, "meta": {}}
def is_edt(dd):
	y = dd.year
	m3 = dt.date(y, 3, 8) + dt.timedelta(days=(6 - dt.date(y, 3, 8).weekday()) % 7)
	n1 = dt.date(y, 11, 1) + dt.timedelta(days=(6 - dt.date(y, 11, 1).weekday()) % 7)
	return m3 <= dd < n1
def signal(top, end, m=1.05):
	for x in sorted(BS):
		if top <= x <= end and BS[x][2] <= m * CV[prev(x)]:
			th = m * CV[prev(x)]
			hit = next((h for h, y in HR if h[:10] == x and y <= th), None)
			ch = 20 if is_edt(dt.date.fromisoformat(x)) else 21
			if x in D and hit and int(hit[11:13]) < ch:
				return x, x, hit
			nxt = next(t for t in TD if t > x)
			return x, nxt, hit
	return None, None, None
for name, (top, end) in CYC.items():
	seg = [D[t] for t in TD if top <= t <= end and ok(D[t])]
	mlo = min(seg, key=lambda r: r["mstr_low"])
	mn_ev = min(seg, key=lambda r: r["mnav_ev"])
	eqs = [r for r in seg if r.get("mnav_eq")]
	mn_eq = min(eqs, key=lambda r: r["mnav_eq"]) if eqs else None
	bseg = [x for x in sorted(BS) if top <= x <= end]
	blo = min(bseg, key=lambda x: BS[x][2])
	neg_eq = [r["date"] for r in seg if r.get("eq_nav_per_share") is not None and r["eq_nav_per_share"] <= 0]
	sig, entry, hit = signal(top, end)
	out["cycles"][name] = {"btc_low": [blo, BS[blo][2]], "mstr_low": [mlo["date"], mlo["mstr_low"]], "mnav_ev_min": [mn_ev["date"], round(mn_ev["mnav_ev"], 3), mn_ev["mstr"]], "mnav_eq_min": [mn_eq["date"], round(mn_eq["mnav_eq"], 3)] if mn_eq else None, "days_equity_nav_negative": len(neg_eq), "first_last_neg_eq": [neg_eq[0], neg_eq[-1]] if neg_eq else None, "lag_days": {"mstr_low_minus_btc_low": (dt.date.fromisoformat(mlo["date"]) - dt.date.fromisoformat(blo)).days, "mnav_ev_min_minus_btc_low": (dt.date.fromisoformat(mn_ev["date"]) - dt.date.fromisoformat(blo)).days}, "cvdd105_signal": sig, "first_executable_close": entry, "first_hour_below": hit, "mnav_ev_at_entry": round(D[entry]["mnav_ev"], 3) if entry else None, "mstr_at_entry": D[entry]["mstr"] if entry else None, "mnav_ev_range_from_entry": [round(min(r["mnav_ev"] for r in seg if r["date"] >= entry), 3), round(max(r["mnav_ev"] for r in seg if r["date"] >= entry), 3)] if entry else None}
	runs = []
	W = 20
	for i in range(len(seg) - W):
		a, b = seg[i], seg[i + W]
		br = b["btc_px"] / a["btc_px"] - 1
		mr = b["mnav_ev"] / a["mnav_ev"] - 1
		if br > 0.05 and mr < -0.05:
			runs.append((a["date"], b["date"], round(br * 100, 1), round(mr * 100, 1), round((b["mstr"] / a["mstr"] - 1) * 100, 1)))
	merged = []
	for r in runs:
		if merged and r[0] <= merged[-1]["end"]:
			merged[-1]["end"] = r[1]
			merged[-1]["worst"] = min(merged[-1]["worst"], r[3], key=lambda z: z)
		else:
			merged.append({"start": r[0], "end": r[1], "worst": r[3]})
	for mg in merged:
		a, b = D[mg["start"]], D[mg["end"]]
		mg.update({"btc_chg_pct": round((b["btc_px"] / a["btc_px"] - 1) * 100, 1), "mnav_ev_chg_pct": round((b["mnav_ev"] / a["mnav_ev"] - 1) * 100, 1), "mstr_chg_pct": round((b["mstr"] / a["mstr"] - 1) * 100, 1), "shares_chg_pct": round((b["shares"] / a["shares"] - 1) * 100, 1), "mnav_from_to": [round(a["mnav_ev"], 3), round(b["mnav_ev"], 3)]})
	out["mnav_down_btc_up"][name] = {"rule": f"{W} trading days: BTC > +5% while EV mNAV < -5%", "episodes": merged}
	if not entry:
		continue
	after = [D[t] for t in TD if entry <= t <= end and ok(D[t])]
	cyc_low = min(r["mstr_low"] for r in after)
	def evalbuys(buys, deadline=None):
		fil = sum(w for _, w, _ in buys)
		if not buys:
			return {"filled_pct": 0, "avg": None}
		avg = fil / sum(w / p for _, w, p in buys)
		first = min(dd for dd, _, _ in buys)
		lows = [r["mstr_low"] for r in after if r["date"] >= first]
		return {"filled_pct": round(fil * 100), "avg": round(avg, 2), "avg_vs_cycle_low_pct": round((avg / cyc_low - 1) * 100, 1), "max_dd_from_avg_pct": round((min(lows) / avg - 1) * 100, 1), "first_buy": first, "last_buy": max(dd for dd, _, _ in buys)}
	S = {}
	S["즉시 매수"] = evalbuys([(entry, 1.0, D[entry]["mstr"])])
	for wk in [2, 4, 6, 8, 12]:
		days = [t for t in TD if entry <= t <= end]
		buys, n = [], wk
		start = dt.date.fromisoformat(entry)
		for k in range(n):
			tgt = (start + dt.timedelta(weeks=k)).isoformat()
			t = next((x for x in days if x >= tgt), None)
			if t:
				buys.append((t, 1 / n, D[t]["mstr"]))
		S[f"{wk}주 분할"] = evalbuys(buys)
	ladder = [1.0, 0.9, 0.8, 0.7]
	buys = []
	for lv in ladder:
		t = next((r for r in after if r["mnav_ev"] <= lv), None)
		if t:
			buys.append((t["date"], 0.25, t["mstr"]))
	S["mNAV 1.0/0.9/0.8/0.7 사다리"] = evalbuys(buys)
	dl = (dt.date.fromisoformat(entry) + dt.timedelta(weeks=12)).isoformat()
	b2 = list(buys)
	fil = sum(w for _, w, _ in b2)
	if fil < 1:
		t = next((x for x in TD if x >= dl), None)
		if t:
			b2.append((t, 1 - fil, D[t]["mstr"]))
	S["mNAV 사다리 + 12주 뒤 잔량 매수"] = evalbuys(b2)
	for wk in [8]:
		for cap in [1.0, 1.1]:
			buys = []
			start = dt.date.fromisoformat(entry)
			pend = 0.0
			for k in range(wk):
				pend += 1 / wk
				lo = (start + dt.timedelta(weeks=k)).isoformat()
				hi = (start + dt.timedelta(weeks=k + 1)).isoformat()
				c = next((r for r in after if lo <= r["date"] < hi and r["mnav_ev"] <= cap), None)
				if c:
					buys.append((c["date"], pend, c["mstr"]))
					pend = 0.0
			fl = sum(w for _, w, _ in buys)
			res = evalbuys(buys)
			b3 = list(buys)
			if fl < 0.999:
				t = next((x for x in TD if x >= dl), None)
				if t:
					b3.append((t, 1 - fl, D[t]["mstr"]))
			S[f"{wk}주 분할 + mNAV≤{cap} 조건"] = res
			S[f"{wk}주 분할 + mNAV≤{cap} 조건 + 12주 뒤 잔량"] = evalbuys(b3)
	out["strategies"][name] = {"entry": entry, "cycle_mstr_low_after_entry": cyc_low, "window_end": end, "results": S}
out["meta"] = {"mnav_ev": "(mcap + debt + pref - cash)/(btc_held*btc_px); mcap uses basic shares (10-Q/10-K cover anchor + 8-K ATM sales after anchor). Converts treated as debt only (no share dilution) to avoid double counting.", "mnav_eq": "mcap/(btc_nav - debt - pref + cash); undefined when equity NAV <= 0", "point_in_time": "each input used only after its SEC acceptance time and before the day's US close (20/21 UTC)", "btc_px": "checkonchain hourly BTC price one hour before US close", "entry_signal": "BTC (Bitstamp) daily low <= 1.05*CVDD(t-1); buy at MSTR close if first hourly price below threshold came before US close, else next trading day close", "limits": ["2 cycles only (2022, 2025-26 ongoing)", "debt = XBRL LongTermDebt carrying value (2020-21 converts carried below face)", "pref = quarterly liquidation preference from 10-Q/XBRL; intra-quarter preferred issuance/repurchase not tracked; FY2025 10-K pref not parsed (2026-02-19..2026-05-06 uses Q3 2025 value)", "cash = XBRL cash & equivalents (includes USD reserve from Dec 2025)", "shares exclude options/RSUs/convert shares"]}
json.dump(out, open(os.path.join(H, "results_mnav.json"), "w"), indent=1)
print(json.dumps(out["cycles"], indent=1))
for n, s in out["strategies"].items():
	print("\n==", n, "entry", s["entry"], "cycle low after entry", s["cycle_mstr_low_after_entry"])
	for k, v in s["results"].items():
		print(f"  {k:34s}", v)
for n, s in out["mnav_down_btc_up"].items():
	print("\n== mNAV down / BTC up", n)
	for e in s["episodes"]:
		print("  ", e)
