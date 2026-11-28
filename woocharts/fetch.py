import csv, io, json, os, datetime as dt, requests
URL = "https://woocharts.com/bitcoin-price-models/data/chart.json"
CM = "https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv"
OUT = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (personal research, 1x/day)"}
r = requests.get(URL, timeout=60, headers=UA)
print("woocharts", r.status_code, len(r.content))
r.raise_for_status()
d = r.json()
with open(os.path.join(OUT, "price_models.json"), "w") as f:
	json.dump(d, f, separators=(",", ":"))
def ser(k):
	return {dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat(): y for x, y in zip(d[k]["x"], d[k]["y"]) if y is not None}
P, C, R = ser("market"), ser("cvdd"), ser("realised")
S = {}
supply_note = "coinmetrics"
try:
	c = requests.get(CM, timeout=60, headers=UA)
	c.raise_for_status()
	for row in csv.DictReader(io.StringIO(c.text)):
		if row.get("SplyCur"):
			S[row["time"][:10]] = float(row["SplyCur"])
except Exception as e:
	supply_note = f"coinmetrics_failed: {e}"
last_s = max(S) if S else None
days = sorted(set(P) & set(C) & set(R))
for day in days:
	if last_s and day > last_s:
		S[day] = S[last_s] + 450 * (dt.date.fromisoformat(day) - dt.date.fromisoformat(last_s)).days
def tier(x):
	if x >= 1.30:
		return "NORMAL"
	if x >= 1.10:
		return "APPROACH"
	if x >= 1.00:
		return "BOTTOM_ZONE"
	return "BELOW_CVDD"
TIER_KO = {"NORMAL": "평상시", "APPROACH": "접근", "BOTTOM_ZONE": "바닥권", "BELOW_CVDD": "CVDD 이탈"}
def snap(day):
	p, cv, rl = P[day], C[day], R[day]
	s = S.get(day)
	bal = rl - cv * 6e6 / s if s else None
	x = p / cv
	return {"date": day, "price": round(p, 2), "cvdd": round(cv, 2), "ratio": round(x, 4), "tier": tier(x), "tier_ko": TIER_KO[tier(x)], "balanced": round(bal, 2) if bal else None, "price_to_balanced": round(p / bal, 4) if bal else None, "realized": round(rl, 2), "price_to_realized": round(p / rl, 4)}
cur = snap(days[-1])
prev = snap(days[-2])
wk = snap(days[-8]) if len(days) > 8 else None
age = (dt.datetime.now(dt.timezone.utc).date() - dt.date.fromisoformat(cur["date"])).days
alert = {
	"generated_at_kst": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
	"source": "woocharts.com/bitcoin-price-models (Glassnode, Willy Woo CVDD)",
	"latest": cur,
	"prev_day": prev,
	"week_ago": wk,
	"tier_changed": cur["tier"] != prev["tier"],
	"gap_to_cvdd_pct": round((cur["ratio"] - 1) * 100, 2),
	"cvdd_7d_change_pct": round((cur["cvdd"] / wk["cvdd"] - 1) * 100, 3) if wk else None,
	"data_age_days": age,
	"stale": age > 3,
	"tiers": {"NORMAL": ">=1.30", "APPROACH": "1.10-1.30", "BOTTOM_ZONE": "1.00-1.10", "BELOW_CVDD": "<1.00"},
	"history_bottoms_ratio": {"2011-11-18": 0.92, "2015-01-14": 1.02, "2018-12-15": 0.99, "2020-03-12": 1.09, "2022-11-21": 1.00},
	"balanced_method": "realized - cvdd*6e6/supply",
	"supply": {"source": supply_note, "last_actual": last_s, "filled_after": "450 BTC/day"},
	"note": "observation only, not part of RSM score",
}
with open(os.path.join(OUT, "cvdd_alert.json"), "w") as f:
	json.dump(alert, f, ensure_ascii=False, indent=1)
print(json.dumps(alert, ensure_ascii=False, indent=1))
