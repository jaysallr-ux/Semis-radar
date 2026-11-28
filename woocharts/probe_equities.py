import json, os, re, time, datetime as dt, requests
OUT = os.path.join(os.path.dirname(__file__), "data", "equities")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
log = {}
def yahoo(sym):
	url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
	r = requests.get(url, params={"period1": 0, "period2": int(time.time()), "interval": "1d", "events": "split,div", "includeAdjustedClose": "true"}, headers=UA, timeout=60)
	r.raise_for_status()
	res = r.json()["chart"]["result"][0]
	q = res["indicators"]["quote"][0]
	adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
	rows = {}
	for i, t in enumerate(res["timestamp"]):
		if q["close"][i] is None:
			continue
		day = dt.datetime.fromtimestamp(t + res["meta"].get("gmtoffset", 0), dt.timezone.utc).date().isoformat()
		rows[day] = [q["open"][i], q["high"][i], q["low"][i], q["close"][i], adj[i] if adj else None]
	splits = res.get("events", {}).get("splits", {})
	return rows, splits
for sym in ["MSTR", "MSTU", "MSTX", "ASST", "IBIT"]:
	try:
		rows, splits = yahoo(sym)
		with open(os.path.join(OUT, f"{sym}_1d.json"), "w") as f:
			json.dump({"note": "yahoo chart v8, o/h/l/c split-adjusted, adjclose", "splits": splits, "ohlc": rows}, f, separators=(",", ":"))
		k = sorted(rows)
		log[sym] = {"n": len(k), "first": k[0], "last": k[-1], "splits": list(splits.values())}
	except Exception as e:
		log[sym] = {"error": str(e)[:300]}
	time.sleep(1)
for name, url in {"strategy_purchases": "https://www.strategy.com/purchases", "strategy_api_kpi": "https://api.strategy.com/btc/bitcoinKpis", "strategy_api_purchases": "https://api.strategy.com/btc/mstrKpiData"}.items():
	try:
		r = requests.get(url, headers=UA, timeout=60)
		log[name] = {"status": r.status_code, "bytes": len(r.content), "ctype": r.headers.get("content-type")}
		if r.ok:
			with open(os.path.join(OUT, name + (".json" if "json" in (r.headers.get("content-type") or "") else ".html")), "wb") as f:
				f.write(r.content)
	except Exception as e:
		log[name] = {"error": str(e)[:300]}
with open(os.path.join(OUT, "log.json"), "w") as f:
	json.dump(log, f, indent=1)
print(json.dumps(log, indent=1))
