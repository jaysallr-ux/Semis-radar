"""OpenDART 키 동작 확인. 키 값은 절대 출력하지 않는다."""
import io
import os
import sys
import zipfile

import requests

key = os.environ.get("DART_API_KEY", "").strip()
if not key:
    sys.exit("DART_API_KEY 비어 있음 (Secrets 이름 확인: DART_API_KEY)")
print(f"키 길이 {len(key)} (OpenDART 키는 보통 40자)")

r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=30, params={
    "crtfc_key": key, "corp_code": "00126380", "bgn_de": "20240101", "end_de": "20241231",
    "pblntf_ty": "A", "page_count": 10})
j = r.json()
print("list.json HTTP", r.status_code, "status", j.get("status"), j.get("message"))
for it in (j.get("list") or [])[:5]:
    print("  ", it.get("corp_name"), it.get("report_nm"), it.get("rcept_dt"), it.get("rcept_no"))

r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=60)
try:
    z = zipfile.ZipFile(io.BytesIO(r.content))
    print("corpCode.xml OK, 파일", z.namelist(), "크기", len(r.content))
except zipfile.BadZipFile:
    print("corpCode.xml 실패:", r.content[:200])
    sys.exit(1)
if j.get("status") != "000":
    sys.exit(1)
