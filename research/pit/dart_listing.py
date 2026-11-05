"""분할·신규상장·표본 시작 공백용 공시 목록 수집 (사전등록 2026-11-06 07:10)

입력 classification/listing_request.csv: code,corp_code,bgn,end,types,why
  types: 공시유형 코드를 | 로 연결 (A 정기, B 주요사항, C 발행공시 등)
출력 classification/listing/filings.csv: code,corp_code,pblntf_ty,rcept_no,rcept_dt,report_nm,corp_name,why
DART 키는 환경변수(DART_API_KEY)로만 받음
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from dart_scan import call  # noqa: E402

CLS = os.path.join(os.path.dirname(__file__), "classification")
OUT = os.path.join(CLS, "listing")


def one(corp_code, bgn, end, ty):
    rows, page = [], 1
    while True:
        j = call("list.json", {"corp_code": corp_code, "bgn_de": bgn, "end_de": end, "pblntf_ty": ty,
                               "page_no": page, "page_count": 100})
        st = (j or {}).get("status", "no_response")
        if st != "000":
            return rows, st
        rows += j.get("list", [])
        if page >= int(j.get("total_page", 1)):
            return rows, "000"
        page += 1


def main():
    os.makedirs(OUT, exist_ok=True)
    req = pd.read_csv(os.path.join(CLS, "listing_request.csv"), dtype=str)
    out, log = [], []
    for r in req.itertuples():
        for ty in r.types.split("|"):
            rows, st = one(r.corp_code, r.bgn, r.end, ty)
            log.append({"code": r.code, "corp_code": r.corp_code, "type": ty, "status": st, "n": len(rows)})
            for x in rows:
                out.append({"code": r.code, "corp_code": r.corp_code, "pblntf_ty": ty, "rcept_no": x["rcept_no"],
                            "rcept_dt": x["rcept_dt"], "report_nm": x["report_nm"].strip(),
                            "corp_name": x.get("corp_name", ""), "why": r.why})
    pd.DataFrame(out).to_csv(os.path.join(OUT, "filings.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(log).to_csv(os.path.join(OUT, "status.csv"), index=False, encoding="utf-8-sig")
    print("목록", len(out), "| 조회", len(log), "| 오류", sum(1 for x in log if x["status"] not in ("000", "013")))


if __name__ == "__main__":
    main()
