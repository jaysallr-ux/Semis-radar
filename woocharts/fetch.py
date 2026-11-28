import json, os, sys, requests
URL = "https://woocharts.com/bitcoin-price-models/data/chart.json"
OUT = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(OUT, exist_ok=True)
r = requests.get(URL, timeout=60, headers={"User-Agent": "Mozilla/5.0 (personal research, 1x/day)"})
print("status", r.status_code, "bytes", len(r.content))
r.raise_for_status()
d = r.json()
with open(os.path.join(OUT, "price_models.json"), "w") as f:
    json.dump(d, f, separators=(",", ":"))
summary = {}
def walk(o, path):
    if isinstance(o, dict):
        if "x" in o and "y" in o and isinstance(o["x"], list):
            summary[path] = {"n": len(o["x"]), "first": [o["x"][0], o["y"][0]] if o["x"] else None, "last": [o["x"][-1], o["y"][-1]] if o["x"] else None}
        for k, v in o.items():
            if k not in ("x", "y"):
                walk(v, f"{path}/{k}" if path else k)
    elif isinstance(o, list):
        for i, v in enumerate(o[:50]):
            walk(v, f"{path}[{i}]")
walk(d, "")
with open(os.path.join(OUT, "summary.json"), "w") as f:
    json.dump(summary, f, indent=1)
print(json.dumps(summary, indent=1))
