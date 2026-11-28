import json, os, time, datetime as dt, requests
OUT = os.path.join(os.path.dirname(__file__), "data", "exchanges")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (personal research)"}
log = {}
def day(ts):
	return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
def save(name, rows, note):
	rows = dict(sorted(rows.items()))
	with open(os.path.join(OUT, f"{name}_1d.json"), "w") as f:
		json.dump({"note": note, "ohlc": rows}, f, separators=(",", ":"))
	log[name] = {"n": len(rows), "first": next(iter(rows), None), "last": next(reversed(rows), None) if rows else None, "note": note}
def bitstamp():
	rows, start = {}, int(dt.datetime(2011, 8, 1, tzinfo=dt.timezone.utc).timestamp())
	now = time.time()
	while start < now:
		r = requests.get("https://www.bitstamp.net/api/v2/ohlc/btcusd/", params={"step": 86400, "limit": 1000, "start": start}, headers=UA, timeout=60)
		r.raise_for_status()
		data = r.json()["data"]["ohlc"]
		if not data:
			break
		for c in data:
			rows[day(int(c["timestamp"]))] = [float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])]
		nxt = int(data[-1]["timestamp"]) + 86400
		if nxt <= start:
			break
		start = nxt
		time.sleep(0.5)
	save("bitstamp", rows, "BTC/USD spot, UTC day")
def coinbase():
	rows = {}
	t = dt.datetime(2015, 7, 20, tzinfo=dt.timezone.utc)
	end = dt.datetime.now(dt.timezone.utc)
	while t < end:
		t2 = min(t + dt.timedelta(days=299), end)
		r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles", params={"granularity": 86400, "start": t.isoformat(), "end": t2.isoformat()}, headers=UA, timeout=60)
		r.raise_for_status()
		for c in r.json():
			rows[day(int(c[0]))] = [float(c[3]), float(c[2]), float(c[1]), float(c[4])]
		t = t2
		time.sleep(0.4)
	save("coinbase", rows, "BTC-USD spot, UTC day")
def binance():
	rows, st = {}, int(dt.datetime(2017, 8, 17, tzinfo=dt.timezone.utc).timestamp() * 1000)
	while True:
		r = requests.get("https://data-api.binance.vision/api/v3/klines", params={"symbol": "BTCUSDT", "interval": "1d", "startTime": st, "limit": 1000}, headers=UA, timeout=60)
		r.raise_for_status()
		data = r.json()
		if not data:
			break
		for c in data:
			rows[day(c[0] // 1000)] = [float(c[1]), float(c[2]), float(c[3]), float(c[4])]
		if len(data) < 1000:
			break
		st = data[-1][0] + 86400000
		time.sleep(0.3)
	save("binance", rows, "BTCUSDT spot, UTC day")
def bitmex():
	rows, start = {}, 0
	while True:
		r = requests.get("https://www.bitmex.com/api/v1/trade/bucketed", params={"binSize": "1d", "symbol": "XBTUSD", "count": 1000, "start": start, "reverse": "false", "partial": "false"}, headers=UA, timeout=60)
		r.raise_for_status()
		data = r.json()
		if not data:
			break
		for c in data:
			if c["low"] is None:
				continue
			close_t = dt.datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00"))
			rows[(close_t - dt.timedelta(days=1)).date().isoformat()] = [c["open"], c["high"], c["low"], c["close"]]
		if len(data) < 1000:
			break
		start += 1000
		time.sleep(2.5)
	save("bitmex_xbtusd", rows, "XBTUSD perpetual last-trade OHLC (not mark price), UTC day")
for fn in (bitstamp, coinbase, binance, bitmex):
	try:
		fn()
	except Exception as e:
		log[fn.__name__] = {"error": str(e)[:300]}
with open(os.path.join(OUT, "log.json"), "w") as f:
	json.dump(log, f, indent=1)
print(json.dumps(log, indent=1))
