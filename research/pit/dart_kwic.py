"""
빠른 제외 트랙 근거 수집 (작업 방식 변경 2026-11-06, 판정 기준 불변).

회사별로
  1) 기간 내 사업보고서 전부의 「사업의 내용」에서 핵심어(T1) 문맥(앞뒤 60자)을 모두 수집
  2) DART 주요사항보고서(pblntf_ty=B) 중 합병·분할·영업양수도 공시 목록
출력 (커밋): research/pit/classification/kwic/<code>.txt, kwic/kwic_summary.csv
입력: research/pit/classification/kwic_request.csv (code)
  python research/pit/dart_kwic.py --workers 4
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dart_scan import BGN, END, KEY, STATS, T1, call, decode, extract_business  # noqa: E402

CLS = os.path.join(HERE, "classification")
OUT = os.path.join(CLS, "kwic")
CTX = 60
MAX_SNIP_PER_DOC = 40
CORP_EVENT = re.compile(r"합병|분할|영업양수|영업양도|자산양수|자산양도")
TERM = re.compile("|".join(re.escape(t) for t in T1), re.I)


def kwic(text: str):
    text = re.sub(r"\s+", " ", text)
    snips, last = [], -10**9
    for m in TERM.finditer(text):
        if m.start() - last < CTX:  # 같은 문맥 중복 방지
            continue
        last = m.start()
        snips.append(text[max(0, m.start() - CTX): m.end() + CTX])
    return snips


def events(corp_code: str):
    rows, page = [], 1
    while True:
        j = call("list.json", {"corp_code": corp_code, "bgn_de": BGN, "end_de": END, "pblntf_ty": "B",
                               "page_no": page, "page_count": 100})
        if not j or j.get("status") != "000":
            return rows, (j or {}).get("status", "no_response")
        rows += [r for r in j.get("list", []) if CORP_EVENT.search(r.get("report_nm", ""))]
        if page >= int(j.get("total_page", 1)):
            return rows, "000"
        page += 1


def doc_text(rcept_no: str):
    b = call("document.xml", {"rcept_no": rcept_no}, binary=True)
    if b is None:
        return None, None
    z = zipfile.ZipFile(io.BytesIO(b))
    names = z.namelist()
    main = next((n for n in names if os.path.basename(n) == f"{rcept_no}.xml"), None) or \
        max(names, key=lambda n: z.getinfo(n).file_size)
    xml, _ = decode(z.read(main))
    text, found = extract_business(xml)
    return text, hashlib.sha256(b).hexdigest()


def work(code, corp_code, names, docs):
    lines = [f"code={code} corp_code={corp_code} names={names}"]
    ev, st = events(corp_code)
    lines.append(f"[합병·분할·영업양수도 공시] 조회상태={st} 건수={len(ev)}")
    for e in ev:
        lines.append(f"  {e['rcept_dt']} {e['report_nm'].strip()} ({e['rcept_no']})")
    total, failed, years_hit = 0, 0, []
    for d in docs.sort_values("rcept_dt").itertuples():
        text, sha = doc_text(d.rcept_no)
        if text is None:
            failed += 1
            lines.append(f"\n## {d.rcept_dt} {d.report_nm} ({d.rcept_no}) 다운로드 실패")
            continue
        sn = kwic(text)
        total += len(sn)
        if sn:
            years_hit.append(d.rcept_dt[:4])
        lines.append(f"\n## {d.rcept_dt} {d.report_nm} ({d.rcept_no}) sha256={sha[:16]} 핵심어 문맥 {len(sn)}개")
        for s in sn[:MAX_SNIP_PER_DOC]:
            lines.append(f"  - …{s}…")
        if len(sn) > MAX_SNIP_PER_DOC:
            lines.append(f"  (외 {len(sn) - MAX_SNIP_PER_DOC}개 생략)")
    with open(os.path.join(OUT, f"{code}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"code": code, "n_annual": len(docs), "failed": failed, "snippets": total,
            "years_hit": ",".join(years_hit), "corp_events": len(ev), "event_status": st}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if not KEY:
        sys.exit("DART_API_KEY 없음")
    os.makedirs(OUT, exist_ok=True)
    req = pd.read_csv(os.path.join(CLS, "kwic_request.csv"), dtype=str)
    fl = pd.read_csv(os.path.join(CLS, "scan_full", "filings.csv"), dtype=str)
    fr = pd.read_csv(os.path.join(CLS, "scan_full", "filter_result.csv"), dtype=str)
    sm = os.path.join(OUT, "kwic_summary.csv")
    done = pd.read_csv(sm, dtype=str) if os.path.exists(sm) else pd.DataFrame(columns=["code"])
    todo = req[~req.code.isin(done.code)]
    print(f"요청 {len(req)}, 완료 {len(done)}, 남음 {len(todo)}", flush=True)
    rows, lock, t0 = [], threading.Lock(), time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = []
        for code in todo.code:
            f = fr[fr.code == code].iloc[0]
            docs = fl[(fl.code == code) & fl.report_nm.str.startswith("사업보고서")].drop_duplicates("rcept_no")
            futs.append(ex.submit(work, code, f.corp_code, f.names, docs))
        for k, fu in enumerate(as_completed(futs), 1):
            with lock:
                rows.append(fu.result())
            if k % 10 == 0:
                print(f"  {k}/{len(todo)} {time.time() - t0:.0f}s api={STATS}", flush=True)
    out = pd.concat([done, pd.DataFrame(rows)], ignore_index=True)
    out.to_csv(sm, index=False, encoding="utf-8-sig")
    print("완료", len(out), f"{time.time() - t0:.0f}s", STATS)


if __name__ == "__main__":
    main()
