"""DART 진단: 호출별 소요 시간, 문서 안 TITLE 구조. 키 미출력."""
import io, os, re, sys, time, zipfile
import requests
KEY = os.environ["DART_API_KEY"].strip()
API = "https://opendart.fss.or.kr/api"
out = []
def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); out.append(s)
for rno in sys.argv[1:]:
    t = time.time()
    r = requests.get(f"{API}/document.xml", params={"crtfc_key": KEY, "rcept_no": rno}, timeout=180)
    log(f"== {rno} HTTP {r.status_code} {len(r.content)}B {time.time()-t:.1f}s head={r.content[:2]}")
    if r.content[:2] != b"PK":
        log(r.content[:300].decode("utf-8", "ignore")); continue
    z = zipfile.ZipFile(io.BytesIO(r.content))
    log("files:", [(n, z.getinfo(n).file_size) for n in z.namelist()])
    main = max(z.namelist(), key=lambda n: z.getinfo(n).file_size)
    x = z.read(main).decode("utf-8", "ignore")
    tags = re.findall(r"<TITLE[^>]*>.{0,120}?</TITLE>", x, re.S)
    log("TITLE 수", len(tags))
    for tg in tags[:45]:
        log("  ", re.sub(r"\s+", " ", tg)[:200])
    i = x.find("사업의 내용")
    log("첫 '사업의 내용' 주변:", re.sub(r"\s+", " ", x[max(0, i-300):i+200]))
for k in range(3):
    t = time.time()
    r = requests.get(f"{API}/list.json", params={"crtfc_key": KEY, "corp_code": "00164779", "bgn_de": "20150101",
                                                 "end_de": "20261105", "pblntf_ty": "A", "page_count": 100}, timeout=60)
    log(f"list.json {time.time()-t:.1f}s status {r.json().get('status')} total {r.json().get('total_count')}")
os.makedirs("research/pit/classification/diag", exist_ok=True)
open("research/pit/classification/diag/dart_diag.txt", "w", encoding="utf-8").write("\n".join(out))
