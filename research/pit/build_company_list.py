"""
거름망 대상 회사 목록 (사전등록 3-1절 1번). marcap 에서만 만든다.
  python research/pit/build_company_list.py --marcap /path/to/marcap
출력: research/pit/classification/companies.csv (code, names, first_date, last_date, markets, excluded, exclude_reason)
"""
import argparse
import os
import re

import pandas as pd

EXCL_NAME = re.compile(r"스팩|리츠|인프라|선박|기업인수목적")

ap = argparse.ArgumentParser()
ap.add_argument("--marcap", required=True)
a = ap.parse_args()
d = pd.concat([pd.read_parquet(os.path.join(a.marcap, "data", f"marcap-{y}.parquet"),
                               columns=["Code", "Name", "Market", "Date"]) for y in range(2015, 2027)])
d = d[d.Market.isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"])]
g = d.groupby("Code").agg(first_date=("Date", "min"), last_date=("Date", "max"),
                          names=("Name", lambda s: "|".join(dict.fromkeys(s))),
                          markets=("Market", lambda s: "|".join(sorted(set(s))))).reset_index()
g["excluded"] = False
g["exclude_reason"] = ""
pref = ~g.Code.str.endswith("0")
g.loc[pref, ["excluded", "exclude_reason"]] = [True, "우선주(코드 끝자리 0 아님)"]
# 마지막 이름 기준 (스팩 합병 상장사는 과거 이름에 '스팩'이 있으므로 과거 이름으로 빼면 안 됨)
g["last_name"] = g.names.str.split("|").str[-1]
nm = g.last_name.str.contains(EXCL_NAME) & ~g.excluded
g.loc[nm, ["excluded", "exclude_reason"]] = [True, "스팩·리츠·인프라·선박·기업인수목적"]
g["first_date"] = g.first_date.dt.date
g["last_date"] = g.last_date.dt.date
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classification", "companies.csv")
g.to_csv(out, index=False, encoding="utf-8-sig")
print(len(g), "codes /", int((~g.excluded).sum()), "대상 /", g[g.excluded].exclude_reason.value_counts().to_dict())
