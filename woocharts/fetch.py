import json, os, requests
URL = "https://woocharts.com/bitcoin-price-models/data/chart.json"
OUT = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(OUT, exist_ok=True)
r = requests.get(URL, timeout=60, headers={"User-Agent": "Mozilla/5.0 (personal research)"})
print("woocharts", r.status_code, len(r.content))
r.raise_for_status()
with open(os.path.join(OUT, "price_models.json"), "w") as f:
	json.dump(r.json(), f, separators=(",", ":"))
