"""
[첨부추가] 복구 사업보고서의 본문이 원 제출 시점 본문과 같은지 확인할 근거 수집 (09:36 지시, 10:34 보존 요청).

문서마다 기록:
- zip 안 파일 목록(이름·크기·zip 내부 수정시각)
- 본문 파일(<rcept_no>.xml)의 zip 내부 수정시각과 첨부 파일들의 수정시각
- 본문 표지의 작성·제출일 문구(예: "2025년 3월 13일")
- 본문 문서 헤더의 DOCUMENT-NAME / FORMULA-VERSION 등 속성
판정(본문_동일_근거):
- 본문 수정시각 날짜 <= 접수번호 날짜(원 제출일) 이고 첨부 중 하나 이상이 그보다 늦음 → "본문 원 제출 시점, 첨부만 추가"
- 본문 수정시각 날짜 <= 접수번호 날짜 → "본문 원 제출 시점"
- 본문 수정시각이 원 제출일보다 늦음 → "본문 시각 늦음(가용일 재검토)"
- zip 시각이 전부 같은 날(패키징 시각) → "zip 시각 무의미" (표지 날짜로만 판단)
출력: classification/listing/attach_check.csv
  python research/pit/dart_attach_check.py --workers 4
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dart_scan import KEY, STATS, TAG, call, decode  # noqa: E402

CLS = os.path.join(HERE, "classification")
DATE_KO = re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def one(x) -> dict:
    row = {"code": x.code, "rcept_no": x.rcept_no, "rcept_dt": x.rcept_dt, "report_nm": x.report_nm}
    b = call("document.xml", {"rcept_no": x.rcept_no}, binary=True)
    if b is None:
        row["error"] = "download_failed"
        return row
    z = zipfile.ZipFile(io.BytesIO(b))
    infos = z.infolist()
    main = next((i for i in infos if os.path.basename(i.filename) == f"{x.rcept_no}.xml"), None) or max(infos, key=lambda i: i.file_size)
    fmt = lambda i: "%04d%02d%02d%02d%02d" % i.date_time[:5]
    row["files"] = "; ".join(f"{i.filename}:{i.file_size}:{fmt(i)}" for i in infos)
    row["main_file"] = main.filename
    row["main_mtime"] = fmt(main)
    others = [fmt(i) for i in infos if i is not main]
    row["attach_mtime_max"] = max(others) if others else ""
    row["n_files"] = len(infos)
    xml, enc = decode(z.read(main))
    head = xml[:3000]
    m = re.search(r"<DOCUMENT-NAME[^>]*>([^<]*)<", head)
    row["doc_name"] = m.group(1).strip() if m else ""
    m = re.search(r"FORMULA-VERSION[^>]*ADATE=\"(\d+)\"", head)
    row["formula_adate"] = m.group(1) if m else ""
    text = re.sub(r"\s+", " ", TAG.sub(" ", xml[:40000]))
    ds = ["%s%02d%02d" % (a, int(b_), int(c)) for a, b_, c in DATE_KO.findall(text)[:6]]
    row["cover_dates"] = ",".join(ds)
    rd = x.rcept_dt
    md = row["main_mtime"][:8]
    same_day = len({fmt(i)[:8] for i in infos}) == 1
    if same_day and md > rd:
        row["verdict"] = "zip 시각 무의미(전부 같은 날, 원 제출일 이후)"
    elif md <= rd and row["attach_mtime_max"][:8] > rd:
        row["verdict"] = "본문 원 제출 시점, 첨부만 추가"
    elif md <= rd:
        row["verdict"] = "본문 원 제출 시점"
    else:
        row["verdict"] = "본문 시각 늦음(가용일 재검토)"
    row["cover_le_rcept"] = int(any(d <= rd for d in ds)) if ds else ""
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if not KEY:
        sys.exit("DART_API_KEY 없음")
    av = pd.read_csv(os.path.join(CLS, "listing", "annual_availability.csv"), dtype=str)
    t = av[(av.pref == "첨부추가") & (av.pit_relevance == "사용")]
    print("대상", len(t), flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(one, x) for x in t.itertuples()]
        for k, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(f.result())
            except Exception as e:  # noqa: BLE001
                rows.append({"error": repr(e)[:200]})
            if k % 50 == 0:
                print(k, STATS, flush=True)
    d = pd.DataFrame(rows)
    d.to_csv(os.path.join(CLS, "listing", "attach_check.csv"), index=False, encoding="utf-8-sig")
    print(d.get("verdict", pd.Series(dtype=str)).value_counts(dropna=False).to_dict())


if __name__ == "__main__":
    main()
