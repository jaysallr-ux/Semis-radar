"""매트릭스 프로브: 작업 하나당 자막 하나. 결과를 out/<name>.json 에 쓴다."""
import json, os, sys, time
from pathlib import Path
name, vid = sys.argv[1], sys.argv[2]
res = {"name": name, "video_id": vid}
try:
    import requests
    res["runner_ip"] = requests.get("https://api.ipify.org", timeout=10).text
except Exception as e:
    res["runner_ip"] = f"? {type(e).__name__}"
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    t0 = time.time()
    f = YouTubeTranscriptApi().fetch(vid, languages=["en", "en-US", "en-GB"])
    text = " ".join(s.text for s in f)
    res.update(ok=True, chars=len(text), language=getattr(f, "language_code", None), seconds=round(time.time()-t0, 1))
except Exception as e:
    res.update(ok=False, error_type=type(e).__name__)
Path("out").mkdir(exist_ok=True)
Path(f"out/{name}.json").write_text(json.dumps(res))
print(json.dumps(res))
