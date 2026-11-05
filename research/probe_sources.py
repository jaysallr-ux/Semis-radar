"""
v3 2단계 준비: 과거 유니버스 복원용 무료 소스 조사 (데이터 수집 가능성만 확인, 전략 계산 없음)
  - KIND 현재 상장사 업종(KSIC) : FinanceDataReader 'KRX-DESC'
  - 상장폐지 목록 + 업종        : FinanceDataReader 'KRX-DELISTING'
  - 상장폐지 종목 가격           : 'KRX-DELISTING:코드'
결과: research/probe/ 에 CSV + 요약
"""
import json
import os
import traceback

import pandas as pd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe")
os.makedirs(OUT, exist_ok=True)
UNI = json.load(open(os.path.join(os.path.dirname(OUT), "..", "universe.json"), encoding="utf-8"))
KEYS = ["반도체", "특수 목적용 기계", "전자부품", "인쇄회로", "기초 화학", "기타 화학"]
report = {}


def step(name, fn):
    try:
        report[name] = fn()
    except Exception as e:
        report[name] = {"error": repr(e), "trace": traceback.format_exc()[-800:]}
    print(f"== {name}\n{json.dumps(report[name], ensure_ascii=False, indent=1, default=str)[:3000]}\n")


import FinanceDataReader as fdr  # noqa: E402


def listed():
    df = fdr.StockListing("KRX-DESC")
    df.to_csv(os.path.join(OUT, "krx_desc.csv"), index=False, encoding="utf-8-sig")
    ind_col = next((c for c in ["Industry", "Sector"] if c in df.columns), None)
    codes = {s["ticker"] for s in UNI["stocks"]}
    code_col = "Code" if "Code" in df.columns else "Symbol"
    mine = df[df[code_col].isin(codes)][[code_col, "Name"] + [c for c in ["Sector", "Industry"] if c in df.columns]]
    out = {"rows": len(df), "columns": list(df.columns), "industry_col": ind_col,
           "universe28_classification": mine.to_dict("records")}
    if "Products" in df.columns:
        prod = df["Products"].astype(str)
        out["count_products_반도체"] = int(prod.str.contains("반도체").sum())
        out["universe28_products"] = df[df[code_col].isin(codes)][["Name", "Products"]].to_dict("records")
    if ind_col:
        for k in KEYS:
            out[f"count_{k}"] = int(df[ind_col].astype(str).str.contains(k).sum())
        out["sector_counts_top"] = df["Sector"].value_counts().head(5).to_dict() if "Sector" in df.columns else None
    return out


def delisted():
    df = fdr.StockListing("KRX-DELISTING")
    df.to_csv(os.path.join(OUT, "krx_delisting.csv"), index=False, encoding="utf-8-sig")
    out = {"rows": len(df), "columns": list(df.columns)}
    if len(df) == 0:
        out["warning"] = "빈 결과 (FDR 은 실패 시 빈 DataFrame 을 돌려줄 수 있음)"
        return out
    dcol = next(c for c in df.columns if "Delist" in c)
    df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
    since = df[df[dcol] >= "2015-01-01"]
    out["since2015"] = len(since)
    ind_col = next((c for c in ["Industry", "Sector"] if c in df.columns), None)
    out["industry_col"] = ind_col
    if ind_col:
        semi = since[since[ind_col].astype(str).str.contains("반도체")]
        out["since2015_반도체"] = len(semi)
        out["since2015_반도체_sample"] = semi.head(30).to_dict("records")
        out["since2015_특수목적기계"] = int(since[ind_col].astype(str).str.contains("특수 목적용 기계").sum())
    return out


def delisted_price():
    lst = pd.read_csv(os.path.join(OUT, "krx_delisting.csv"), dtype=str)
    code_col = "Symbol" if "Symbol" in lst.columns else lst.columns[0]
    ind_col = next((c for c in ["Industry", "Sector"] if c in lst.columns), None)
    cand = lst[lst[ind_col].astype(str).str.contains("반도체")] if ind_col else lst
    res = []
    for code in cand[code_col].head(3):
        df = fdr.DataReader(f"KRX-DELISTING:{code}", "2015-01-01")
        res.append({"code": code, "rows": len(df), "columns": list(df.columns)[:12],
                    "first": str(df.index.min()) if len(df) else None, "last": str(df.index.max()) if len(df) else None})
    return res


step("listed_KRX-DESC", listed)
step("delisted_list", delisted)
step("delisted_price", delisted_price)
try:
    with open(os.path.join(OUT, "probe_summary.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)
except Exception as e:
    print("summary 저장 실패:", repr(e))
