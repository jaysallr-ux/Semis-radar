"""프로브: youtube-transcript.ai 의 .txt 엔드포인트가 Actions 에서 자막 원문을 돌려주는지."""
import json, time
from pathlib import Path
import requests
VIDS = {"karsan": "YIR7RVLadt8", "bianco": "bygJZ4o0SJE", "constan": "-nDxML0csuM", "dale": "rVz79aPJnqE"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
rows = []
for rnd in (1, 2):
    for name, vid in VIDS.items():
        row = {"round": rnd, "name": name, "video_id": vid}
        try:
            t0 = time.time()
            r = requests.get(f"https://youtube-transcript.ai/transcript/{vid}.txt", headers=UA, timeout=60)
            row.update(status=r.status_code, chars=len(r.text), seconds=round(time.time() - t0, 1),
                       ctype=r.headers.get("content-type"), head=r.text[:160].replace("\n", " "))
            body = r.text
            row["has_timestamps"] = "[0:0" in body or "[0:" in body
        except Exception as e:
            row.update(error=f"{type(e).__name__}: {e}"[:200])
        rows.append(row)
        time.sleep(4)
Path("guru_yt/data").mkdir(parents=True, exist_ok=True)
Path("guru_yt/data/yt_probe_svc.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
print(json.dumps(rows, ensure_ascii=False, indent=1))
