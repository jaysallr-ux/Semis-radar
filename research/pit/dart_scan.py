"""
DART 거름망 스캐너 (사전등록 PREREG_v3_step2.md 3-1, 3-2절).

  python research/pit/dart_scan.py --mode sample   # 검증 표본만
  python research/pit/dart_scan.py --mode full     # 대상 전체

입력: research/pit/classification/companies.csv, validation_sample.csv
출력 (커밋): research/pit/classification/scan_<mode>/
  corp_map.csv       종목코드 → DART corp_code, 매핑 방법
  filings.csv        기간 내 정기보고서 목록
  dart_manifest.csv  받은 문서별 rcept_no, 접수일, SHA-256, 바이트, 사업의 내용 추출 여부, 키워드 수
  filter_result.csv  회사별 거름망 결과
출력 (아티팩트, 커밋 안 함): scan_<mode>/raw/<rcept_no>.zip, scan_<mode>/text/<rcept_no>.txt
키는 환경변수 DART_API_KEY. 출력 금지.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CLS = os.path.join(HERE, "classification")
API = "https://opendart.fss.or.kr/api"
BGN, END = "20150101", "20261105"
T1 = ["반도체", "웨이퍼", "파운드리", "메모리", "DRAM", "디램", "NAND", "낸드", "HBM", "팹리스"]
PASS_MIN = 3
PERIODIC = re.compile(r"^(사업보고서|반기보고서|분기보고서)")
ANNUAL = re.compile(r"^사업보고서")
KEY = os.environ.get("DART_API_KEY", "").strip()
_TL = threading.local()
STATS = {"calls": 0, "limit_020": 0, "sys_800_900": 0, "exceptions": 0, "wait_seconds": 0.0}
_SL = threading.Lock()


def stat(k, v=1):
    with _SL:
        STATS[k] += v


def sess() -> requests.Session:
    if not hasattr(_TL, "s"):
        _TL.s = requests.Session()
    return _TL.s


def norm_name(s: str) -> str:
    s = re.sub(r"\(주\)|주식회사|㈜|\s", "", str(s))
    return s.upper()


def call(path, params, binary=False, tries=6):
    p = dict(params, crtfc_key=KEY)
    for k in range(tries):
        try:
            stat("calls")
            r = sess().get(f"{API}/{path}", params=p, timeout=180)
            if binary:
                if r.content[:2] == b"PK":
                    return r.content
                # 오류는 JSON/XML 텍스트로 옴
                txt = r.content[:300].decode("utf-8", "ignore")
                if "020" in txt:  # 요청 제한
                    stat("limit_020"); stat("wait_seconds", 30 * (k + 1))
                    time.sleep(30 * (k + 1))
                    continue
                return None
            j = r.json()
            if j.get("status") in ("020", "800", "900"):  # 요청 제한 / 시스템 점검 / 기타 오류 → 대기 후 재시도
                stat("limit_020" if j.get("status") == "020" else "sys_800_900"); stat("wait_seconds", 20 * (k + 1))
                time.sleep(20 * (k + 1))
                continue
            return j
        except Exception:
            stat("exceptions"); stat("wait_seconds", 10 * (k + 1))
            time.sleep(10 * (k + 1))
    return None


def corp_codes() -> pd.DataFrame:
    b = call("corpCode.xml", {}, binary=True)
    z = zipfile.ZipFile(io.BytesIO(b))
    root = ET.fromstring(z.read(z.namelist()[0]))
    rows = [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("list")]
    return pd.DataFrame(rows)


def map_companies(comp: pd.DataFrame, cc: pd.DataFrame) -> pd.DataFrame:
    by_stock = {r.stock_code: r for r in cc[cc.stock_code != ""].itertuples()}
    by_name: dict = {}
    for r in cc.itertuples():
        by_name.setdefault(norm_name(r.corp_name), []).append(r)
    out = []
    for r in comp.itertuples():
        rec = {"code": r.Code, "names": r.names, "corp_code": "", "corp_name": "", "method": "미매핑", "candidates": ""}
        if r.Code in by_stock:
            m = by_stock[r.Code]
            rec.update(corp_code=m.corp_code, corp_name=m.corp_name, method="종목코드")
        else:
            hits = {}
            for nm in reversed(str(r.names).split("|")):  # 마지막 이름부터
                for m in by_name.get(norm_name(nm), []):
                    hits[m.corp_code] = m
            if len(hits) == 1:
                m = list(hits.values())[0]
                rec.update(corp_code=m.corp_code, corp_name=m.corp_name, method="회사명")
            elif len(hits) > 1:
                rec.update(method="회사명_중복", candidates="|".join(f"{m.corp_code}:{m.corp_name}" for m in hits.values()))
        out.append(rec)
    return pd.DataFrame(out)


def filings(corp_code: str):
    """(정기보고서 목록, 상태). 상태: '000' 정상 / '013' 조회 결과 없음(진짜 공시 없음) / 그 밖 = 조회 오류"""
    rows, page, status = [], 1, None
    while True:
        j = call("list.json", {"corp_code": corp_code, "bgn_de": BGN, "end_de": END, "pblntf_ty": "A",
                               "page_no": page, "page_count": 100})
        status = (j or {}).get("status", "no_response")
        if status != "000":
            break
        rows += j.get("list", [])
        if page >= int(j.get("total_page", 1)):
            break
        page += 1
    if rows and status != "000":
        status = "partial_" + status
    elif rows:
        status = "000"
    return [r for r in rows if PERIODIC.match(r.get("report_nm", "").strip())
            and "[기재정정]" not in r.get("report_nm", "") and "[첨부" not in r.get("report_nm", "")], status


def pick_sample_docs(fl: list[dict]) -> list[dict]:
    """사전등록: 사업보고서 중 가장 이른·늦은·중간. 없으면 정기보고서 중 가장 이른·늦은"""
    ann = sorted([f for f in fl if ANNUAL.match(f["report_nm"].strip())], key=lambda f: f["rcept_dt"])
    pool = ann if ann else sorted(fl, key=lambda f: f["rcept_dt"])
    if not pool:
        return []
    pick = [pool[0], pool[-1]]
    if ann and len(ann) >= 3:
        d0 = date.fromisoformat(f"{ann[0]['rcept_dt'][:4]}-{ann[0]['rcept_dt'][4:6]}-{ann[0]['rcept_dt'][6:]}")
        d1 = date.fromisoformat(f"{ann[-1]['rcept_dt'][:4]}-{ann[-1]['rcept_dt'][4:6]}-{ann[-1]['rcept_dt'][6:]}")
        mid = d0 + (d1 - d0) / 2
        pick.append(min(ann[1:-1], key=lambda f: abs(
            date.fromisoformat(f"{f['rcept_dt'][:4]}-{f['rcept_dt'][4:6]}-{f['rcept_dt'][6:]}") - mid)))
    seen, out = set(), []
    for f in pick:
        if f["rcept_no"] not in seen:
            seen.add(f["rcept_no"])
            out.append(f)
    return out


TAG = re.compile(r"<[^>]+>")
SEC_TITLE = re.compile(r"<TITLE[^>]*>\s*([^<]*)</TITLE>", re.I)


def decode(b: bytes) -> tuple[str, str]:
    """(텍스트, 사용한 인코딩). 2015~2020년대 초 문서는 utf-8 선언이지만 실제로는 EUC-KR(cp949)인 경우가 있음"""
    try:
        return b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return b.decode("cp949"), "cp949"
    except UnicodeDecodeError:
        return b.decode("cp949", "replace"), "cp949-replace"


ASSOC = re.compile(r'<TITLE[^>]*AASSOCNOTE="(D-0-\d+)-0-0"[^>]*>', re.I)


def extract_business(xml: str):
    """「사업의 내용」 장 텍스트. (text, found)
    1순위: DART 표준 구조 코드 AASSOCNOTE="D-0-2-0-0"(II. 사업의 내용) ~ 다음 대분류 D-0-3/4-0-0
    2순위: TITLE 글자 '사업의 내용' ~ 다음 로마숫자 대분류"""
    marks = [(m.start(), m.group(1)) for m in ASSOC.finditer(xml)]
    st = next((pos for pos, c in marks if c == "D-0-2"), None)
    if st is not None:
        en = next((pos for pos, c in marks if pos > st and c != "D-0-2"), len(xml))
        return TAG.sub(" ", xml[st:en]), True
    titles = [(m.start(), m.group(1).strip()) for m in SEC_TITLE.finditer(xml)]
    start = None
    for k, (pos, t) in enumerate(titles):
        if re.search(r"사업의\s*내용", t) and not re.search(r"^\s*\d+\.", t):
            start = k
            break
    if start is None:
        return TAG.sub(" ", xml), False
    end = len(xml)
    for pos, t in titles[start + 1:]:
        # 다음 대분류(로마숫자 또는 'III.' 등)에서 끝
        if re.match(r"^\s*(Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|Ⅵ|Ⅶ|Ⅷ|Ⅸ|Ⅹ|I{1,3}|IV|V|VI{0,3}|IX|X)\s*[\.．]", t):
            end = pos
            break
    return TAG.sub(" ", xml[titles[start][0]:end]), True


def count_terms(text: str) -> dict:
    up = text.upper()
    return {t: up.count(t.upper()) for t in T1}


def fetch_doc(rcept_no: str, raw_dir: str, text_dir: str):
    b = call("document.xml", {"rcept_no": rcept_no}, binary=True)
    if b is None:
        return None
    sha = hashlib.sha256(b).hexdigest()
    with open(os.path.join(raw_dir, f"{rcept_no}.zip"), "wb") as f:
        f.write(b)
    z = zipfile.ZipFile(io.BytesIO(b))
    names = z.namelist()
    # 본문 = 파일명이 정확히 <rcept_no>.xml (앞에 '/' 붙는 경우 있음). _00760 등은 첨부 감사보고서
    main = next((n for n in names if os.path.basename(n) == f"{rcept_no}.xml"), None) or \
        max(names, key=lambda n: z.getinfo(n).file_size)
    xml, enc = decode(z.read(main))
    text, found = extract_business(xml)
    text = re.sub(r"\s+", " ", text)
    with open(os.path.join(text_dir, f"{rcept_no}.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return {"sha256": sha, "bytes": len(b), "main_file": main, "encoding": enc, "section_found": int(found),
            "section_fallback": int(not found), "text_chars": len(text), **{f"kw_{k}": v for k, v in count_terms(text).items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sample", "full"], required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-minutes", type=float, default=330)
    ap.add_argument("--push-every-min", type=float, default=0, help=">0 이면 중간 결과를 git commit/push (Actions 전용)")
    a = ap.parse_args()
    if not KEY:
        sys.exit("DART_API_KEY 없음")
    out = os.path.join(CLS, f"scan_{a.mode}")
    raw_dir, text_dir = os.path.join(out, "raw"), os.path.join(out, "text")
    for d in (out, raw_dir, text_dir):
        os.makedirs(d, exist_ok=True)

    comp = pd.read_csv(os.path.join(CLS, "companies.csv"), dtype=str)
    comp = comp[comp.excluded == "False"]
    if a.mode == "sample":
        vs = pd.read_csv(os.path.join(CLS, "validation_sample.csv"), dtype=str)
        comp = comp[comp.Code.isin(vs.code)]
    if a.limit:
        comp = comp.head(a.limit)
    print(f"대상 {len(comp)}개사")

    cc = corp_codes()
    cmap = map_companies(comp, cc)
    cmap.to_csv(os.path.join(out, "corp_map.csv"), index=False, encoding="utf-8-sig")
    print("매핑:", cmap.method.value_counts().to_dict())

    # 이어하기: 이미 끝난 회사는 건너뜀
    def load(name):
        f = os.path.join(out, name)
        return pd.read_csv(f, dtype=str).to_dict("records") if os.path.exists(f) else []
    fl_rows, man_rows, res_rows = load("filings.csv"), load("dart_manifest.csv"), load("filter_result.csv")
    # 재시도 대상: 목록 조회 오류, 문서 다운로드 실패, 상태 기록이 없는 옛 '문서없음'(조회 오류였을 수 있음)
    def needs_retry(r):
        st = str(r.get("list_status", ""))
        if r.get("filter") == "목록오류" or st.startswith("partial"):
            return True
        if r.get("filter") == "문서없음" and st in ("", "nan"):
            return True
        return False
    retry = {r["code"] for r in res_rows if needs_retry(r)}
    res_rows = [r for r in res_rows if r["code"] not in retry]
    fl_rows = [r for r in fl_rows if r["code"] not in retry]
    man_rows = [r for r in man_rows if r["code"] not in retry]
    done_codes = {r["code"] for r in res_rows}
    print(f"재시도 대상 {len(retry)}")
    todo = [r for r in cmap.itertuples() if r.code not in done_codes]
    print(f"이어하기: 완료 {len(done_codes)}, 남음 {len(todo)}")
    t0 = time.time()
    deadline = t0 + a.max_minutes * 60
    lock = threading.Lock()

    def work(r):
        if time.time() > deadline:
            return None
        res = {"code": r.code, "names": r.names, "corp_code": r.corp_code, "map_method": r.method,
               "n_periodic": 0, "n_docs": 0, "max_t1": "", "any_fallback": 0, "filter": ""}
        fls, mans = [], []
        if not r.corp_code:
            res["filter"] = "미매핑"
            return res, fls, mans
        fl, lst = filings(r.corp_code)
        res["list_status"] = lst
        if lst not in ("000", "013"):
            res["filter"] = "목록오류"
            return res, fls, mans
        for f in fl:
            fls.append({"code": r.code, "corp_code": r.corp_code, "rcept_no": f["rcept_no"], "rcept_dt": f["rcept_dt"],
                        "report_nm": f["report_nm"].strip(), "corp_name": f.get("corp_name", "")})
        res["n_periodic"] = len(fl)
        docs = pick_sample_docs(fl)
        best, fb = None, 0
        for f in docs:
            t1s = time.time()
            m = fetch_doc(f["rcept_no"], raw_dir, text_dir)
            row = {"code": r.code, "corp_code": r.corp_code, "rcept_no": f["rcept_no"], "rcept_dt": f["rcept_dt"],
                   "report_nm": f["report_nm"].strip(), "purpose": "filter_sample", "seconds": round(time.time() - t1s, 1)}
            if m is None:
                row["error"] = "download_failed"
            else:
                row.update(m)
                t1 = sum(v for kk, v in m.items() if kk.startswith("kw_"))
                row["t1_total"] = t1
                best = t1 if best is None else max(best, t1)
                fb |= m["section_fallback"]
            mans.append(row)
        res["n_docs"] = len(docs)
        res["any_fallback"] = fb
        if best is None:
            # 공시는 있는데 문서를 하나도 못 받았으면 다운로드 오류 → 재시도 대상
            res["filter"] = "문서없음" if not docs else "목록오류"
        else:
            res["max_t1"] = best
            res["filter"] = "통과" if best >= PASS_MIN else "탈락"
        return res, fls, mans

    last_push = [time.time()]
    n_start = len(res_rows)

    def save(push=False):
        pd.DataFrame(fl_rows).to_csv(os.path.join(out, "filings.csv"), index=False, encoding="utf-8-sig")
        pd.DataFrame(man_rows).to_csv(os.path.join(out, "dart_manifest.csv"), index=False, encoding="utf-8-sig")
        pd.DataFrame(res_rows).to_csv(os.path.join(out, "filter_result.csv"), index=False, encoding="utf-8-sig")
        el = time.time() - t0
        done_now = len(res_rows) - n_start
        rate = done_now / el * 60 if el > 0 else 0
        prog = {"updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "total": len(cmap),
                "done": len(res_rows), "remaining": len(cmap) - len(res_rows), "this_run_done": done_now,
                "this_run_minutes": round(el / 60, 1), "companies_per_min": round(rate, 1),
                "eta_minutes": round((len(cmap) - len(res_rows)) / rate, 0) if rate > 0 else None,
                "filter_counts": pd.Series([r["filter"] for r in res_rows]).value_counts().to_dict(),
                "api": {k: (round(v) if isinstance(v, float) else v) for k, v in STATS.items()}}
        with open(os.path.join(out, "progress.json"), "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False, indent=1)
        if push and a.push_every_min > 0:
            os.system(f'cd "{os.path.dirname(os.path.dirname(os.path.dirname(HERE)))}" && git add research/pit/classification/scan_{a.mode} '
                      f'&& git commit -q -m "dart scan progress {prog["done"]}/{prog["total"]}" '
                      f'&& (git pull -q --rebase || true) && git push -q || true')

    n_done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(work, r) for r in todo]
        for fu in as_completed(futs):
            got = fu.result()
            if got is None:
                continue
            res, fls, mans = got
            with lock:
                res_rows.append(res)
                fl_rows.extend(fls)
                man_rows.extend(mans)
                n_done += 1
                if n_done % 25 == 0:
                    print(f"  {n_done}/{len(todo)} {time.time() - t0:.0f}s api={STATS}", flush=True)
                    do_push = a.push_every_min > 0 and time.time() - last_push[0] > a.push_every_min * 60
                    save(push=do_push)
                    if do_push:
                        last_push[0] = time.time()
    save()
    rr = pd.DataFrame(res_rows)
    print("거름망:", rr["filter"].value_counts().to_dict(), f"이번 실행 {time.time() - t0:.0f}s, 남음 {len(cmap) - len(rr)}")
    if "list_status" in rr:
        print("목록 조회 상태:", rr["list_status"].astype(str).value_counts().to_dict())
    with open(os.path.join(out, "scan_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"mode": a.mode, "bgn": BGN, "end": END, "T1": T1, "pass_min": PASS_MIN,
                   "n_companies": len(cmap), "n_done": len(rr), "complete": len(rr) >= len(cmap)},
                  f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
