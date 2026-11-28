import base64, json, os, re, requests
import numpy as np
OUT = os.path.join(os.path.dirname(__file__), "data", "probe")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (personal research)"}
PAGES = {
	"onchainoriginals": "https://charts-cdn.checkonchain.com/btconchain/pricing/pricing_onchainoriginals/pricing_onchainoriginals_light.html",
	"costbasisoriginals": "https://charts-cdn.checkonchain.com/btconchain/pricing/pricing_costbasisoriginals/pricing_costbasisoriginals_light.html",
}
def arr(v):
	if isinstance(v, dict) and "bdata" in v:
		a = np.frombuffer(base64.b64decode(v["bdata"]), dtype=np.dtype(v["dtype"]))
		if "shape" in v:
			a = a.reshape([int(s) for s in str(v["shape"]).split(",")])
		return a.tolist()
	return v
report = {}
for key, url in PAGES.items():
	r = requests.get(url, timeout=90, headers=UA)
	info = {"status": r.status_code, "bytes": len(r.content)}
	if r.ok:
		t = r.text
		m = re.search(r"Plotly\.newPlot\(\s*[\"'][^\"']+[\"']\s*,\s*", t)
		if m:
			dec = json.JSONDecoder()
			traces, _ = dec.raw_decode(t, m.end())
			out = {}
			for tr in traces:
				name = tr.get("name") or f"trace{len(out)}"
				x, y = arr(tr.get("x")), arr(tr.get("y"))
				if isinstance(x, list) and isinstance(y, list):
					out[name] = {"x": x, "y": y}
			info["traces"] = {k: {"n": len(v["x"]), "last": [v["x"][-1], v["y"][-1]] if v["x"] else None} for k, v in out.items()}
			with open(os.path.join(OUT, f"coc_{key}.json"), "w") as f:
				json.dump(out, f, separators=(",", ":"))
		else:
			info["note"] = "no Plotly.newPlot"
			info["head"] = t[:1500]
	report[key] = info
with open(os.path.join(OUT, "coc_report.json"), "w") as f:
	json.dump(report, f, indent=1, default=str)
print(json.dumps(report, indent=1, default=str))
