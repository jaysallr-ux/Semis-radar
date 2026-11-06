"""
3차 누락 점검 + 다운로드 실패 재확보.

- 대상 A: 거름망 탈락사 중 KIND 업종이 반도체 인접인 회사들의 '아직 안 받은' 사업보고서 전부
  → 한 해라도 T1 >= 3 이면 '재통과' (중간 연도에만 반도체였던 회사 찾기)
- 대상 B: 1차 스캔에서 다운로드 실패한 문서
출력: research/pit/classification/scan_recheck/ (recheck_manifest.csv, recheck_result.csv, progress.json)
  python research/pit/dart_recheck.py --workers 4 --max-minutes 300 [--push-every-min 10]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dart_scan import KEY, PASS_MIN, STATS, fetch_doc  # noqa: E402

CLS = os.path.join(HERE, "classification")
ROOT = os.path.dirname(os.path.dirname(HERE))
ADJ = ("반도체|전자부품|특수 목적용 기계|기초 화학|기타 화학|측정, 시험|유리|기타 비금속|기계장비 및 관련 물품 도매|"
       "일반 목적용 기계|전동기|절연선|컴퓨터 및 주변|통신 및 방송 장비|영상 및 음향|전기장비|플라스틱|금속|"
       "IT부품|전기·전자|기계·장비|화학|비금속광물|기타제조")


def targets() -> pd.DataFrame:
    r = pd.read_csv(os.path.join(CLS, "scan_full", "filter_result.csv"), dtype=str)
    m = pd.read_csv(os.path.join(CLS, "scan_full", "dart_manifest.csv"), dtype=str)
    f = pd.read_csv(os.path.join(CLS, "scan_full", "filings.csv"), dtype=str)
    L = pd.read_csv(os.path.join(ROOT, "research", "probe", "krx_desc.csv"), dtype=str)
    D = pd.read_csv(os.path.join(ROOT, "research", "probe", "krx_delisting.csv"), dtype=str)
    ind = dict(zip(L.Code, L.Industry))
    ind.update({k: v for k, v in zip(D.Symbol, D.Industry) if k not in ind})
    fail = r[r["filter"] == "탈락"].copy()
    fail["industry"] = fail.code.map(ind).fillna("")
    adj = fail[fail.industry.str.contains(ADJ)]
    ann = f[f.report_nm.str.startswith("사업보고서") & f.code.isin(adj.code) & ~f.rcept_no.isin(m.rcept_no)].copy()
    ann["reason"] = "3차점검_중간연도"
    err = m[m.error.notna()][["code", "corp_code", "rcept_no", "rcept_dt", "report_nm"]].copy()
    err["reason"] = "다운로드실패_재시도"
    t = pd.concat([err, ann[["code", "corp_code", "rcept_no", "rcept_dt", "report_nm", "reason"]]], ignore_index=True)
    return t.drop_duplicates("rcept_no")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-minutes", type=float, default=300)
    ap.add_argument("--push-every-min", type=float, default=0)
    ap.add_argument("--req", default="", help="대상 문서 목록 csv(classification/ 기준). 주면 targets() 대신 사용")
    ap.add_argument("--out-sub", default="scan_recheck", help="출력 폴더명(classification/ 아래, scan_ 로 시작)")
    a = ap.parse_args()
    if not KEY:
        sys.exit("DART_API_KEY 없음")
    out = os.path.join(CLS, a.out_sub)
    raw, txt = os.path.join(out, "raw"), os.path.join(out, "text")
    for d in (out, raw, txt):
        os.makedirs(d, exist_ok=True)
    if a.req:
        t = pd.read_csv(os.path.join(CLS, a.req), dtype=str)
        t["reason"] = t.get("why", "요청목록").fillna("요청목록") if "reason" not in t else t.reason
        t = t[["code", "corp_code", "rcept_no", "rcept_dt", "report_nm", "reason"]].drop_duplicates("rcept_no")
    else:
        t = targets()
    mf = os.path.join(out, "recheck_manifest.csv")
    rows = pd.read_csv(mf, dtype=str).to_dict("records") if os.path.exists(mf) else []
    done = {r["rcept_no"] for r in rows if not (isinstance(r.get("error"), str) and r.get("error"))}
    rows = [r for r in rows if r["rcept_no"] in done]
    todo = t[~t.rcept_no.isin(done)]
    print(f"대상 문서 {len(t)} (회사 {t.code.nunique()}), 완료 {len(done)}, 남음 {len(todo)}", flush=True)
    t0 = time.time()
    deadline = t0 + a.max_minutes * 60
    lock = threading.Lock()
    last_push = [t0]

    def work(x):
        if time.time() > deadline:
            return None
        s = time.time()
        m = fetch_doc(x.rcept_no, raw, txt)
        row = {"code": x.code, "corp_code": x.corp_code, "rcept_no": x.rcept_no, "rcept_dt": x.rcept_dt,
               "report_nm": x.report_nm, "reason": x.reason, "seconds": round(time.time() - s, 1)}
        if m is None:
            row["error"] = "download_failed"
        else:
            row.update(m)
            row["t1_total"] = sum(v for k, v in m.items() if k.startswith("kw_"))
        return row

    def save(push=False):
        df = pd.DataFrame(rows)
        df.to_csv(mf, index=False, encoding="utf-8-sig")
        res = []
        if len(df) and "t1_total" in df:
            df["t1"] = pd.to_numeric(df.t1_total, errors="coerce")
            for code, g in df.groupby("code"):
                ok = g[g.error.isna()] if "error" in g else g
                res.append({"code": code, "docs": len(g), "failed": len(g) - len(ok),
                            "max_t1": ok.t1.max() if len(ok) else None,
                            "max_t1_rcept_no": ok.loc[ok.t1.idxmax(), "rcept_no"] if len(ok) and ok.t1.notna().any() else "",
                            "recheck": ("재통과" if len(ok) and ok.t1.max() >= PASS_MIN else ("판정보류_다운로드실패" if len(ok) < len(g) else "탈락유지"))})
        pd.DataFrame(res).to_csv(os.path.join(out, "recheck_result.csv"), index=False, encoding="utf-8-sig")
        prog = {"updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "target_docs": len(t),
                "done_docs": len(rows), "remaining": len(t) - len(rows), "minutes": round((time.time() - t0) / 60, 1),
                "recheck_counts": pd.Series([x["recheck"] for x in res]).value_counts().to_dict() if res else {},
                "api": {k: (round(v) if isinstance(v, float) else v) for k, v in STATS.items()}}
        json.dump(prog, open(os.path.join(out, "progress.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if push and a.push_every_min > 0:
            os.system(f'cd "{ROOT}" && git add research/pit/classification/{a.out_sub} && git commit -q -m "recheck progress {len(rows)}/{len(t)}" '
                      f'&& (git pull -q --rebase || true) && git push -q || true')

    n = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(work, x) for x in todo.itertuples()]
        for fu in as_completed(futs):
            row = fu.result()
            if row is None:
                continue
            with lock:
                rows.append(row)
                n += 1
                if n % 50 == 0:
                    do = a.push_every_min > 0 and time.time() - last_push[0] > a.push_every_min * 60
                    save(push=do)
                    if do:
                        last_push[0] = time.time()
                    print(f"  {n}/{len(todo)} {time.time() - t0:.0f}s api={STATS}", flush=True)
    save()
    print("완료", json.load(open(os.path.join(out, "progress.json"), encoding="utf-8")))


if __name__ == "__main__":
    main()
