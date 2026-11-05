"""
검토 대기열 (사전등록 3-3절). 수익률은 계산하지 않는다. 시총(marcap)으로 순위만 매긴다.

  python research/pit/build_review_queue.py --marcap /path/to/marcap [--excluded classification/excluded_confirmed.csv]

- 대상: 거름망 '통과' + '문서없음' + '미매핑' (뒤의 둘은 수동 확인 필요라 포함)
- 매월 마지막 거래일, 그날 상장된 대상 회사들을 시총 순으로 순위. 제외 확정 회사는 순위 계산에서 뺀다
- best_rank = 기간 중 최고 순위. batch = 1(≤40), 2(41~80), 3(81~120), 4(그 밖)
- 제외 확정이 늘면 다시 돌린다 → 남은 회사들의 순위가 올라감 (반복 재평가)
출력: research/pit/classification/review_queue.csv, review_queue_meta.json
"""
import argparse
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CLS = os.path.join(HERE, "classification")
TOP = 40

ap = argparse.ArgumentParser()
ap.add_argument("--marcap", required=True)
ap.add_argument("--scan", default=os.path.join(CLS, "scan_full"))
ap.add_argument("--excluded", default=os.path.join(CLS, "excluded_confirmed.csv"))
a = ap.parse_args()

fr = pd.read_csv(os.path.join(a.scan, "filter_result.csv"), dtype=str)
cand = fr[fr["filter"].isin(["통과", "문서없음", "미매핑"])].copy()
excl = set()
if os.path.exists(a.excluded):
    excl = set(pd.read_csv(a.excluded, dtype=str).code)
cand = cand[~cand.code.isin(excl)]

d = pd.concat([pd.read_parquet(os.path.join(a.marcap, "data", f"marcap-{y}.parquet"),
                               columns=["Code", "Marcap", "Market", "Date"]) for y in range(2015, 2027)])
d = d[d.Market.isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"]) & d.Code.isin(cand.code)]
d["Date"] = pd.to_datetime(d["Date"])
all_dates = pd.DatetimeIndex(sorted(d.Date.unique()))
per = all_dates.to_period("M")
month_ends = all_dates[np.r_[per[:-1] != per[1:], False]]  # 마지막 달(진행 중)은 제외
m = d[d.Date.isin(month_ends)].copy()
m["rank"] = m.groupby("Date").Marcap.rank(ascending=False, method="first").astype(int)

g = m.groupby("Code")
summ = pd.DataFrame({
    "best_rank": g["rank"].min(),
    "first_month_top40": m[m["rank"] <= TOP].groupby("Code").Date.min().dt.strftime("%Y-%m"),
    "months_top40": m[m["rank"] <= TOP].groupby("Code").size(),
    "months_listed_at_month_end": g.size(),
}).reset_index().rename(columns={"Code": "code"})
q = cand.merge(summ, on="code", how="left")
q["months_top40"] = q.months_top40.fillna(0).astype(int)
q["batch"] = np.select([q.best_rank <= 40, q.best_rank <= 80, q.best_rank <= 120], [1, 2, 3], 4)
q.loc[q.best_rank.isna(), "batch"] = 4
q["status"] = "미검토"
q = q.sort_values(["batch", "best_rank"], na_position="last")
cols = ["batch", "best_rank", "first_month_top40", "months_top40", "months_listed_at_month_end", "code", "names",
        "corp_code", "map_method", "filter", "max_t1", "n_periodic", "status"]
q[cols].to_csv(os.path.join(CLS, "review_queue.csv"), index=False, encoding="utf-8-sig")
meta = {"n_candidates": int(len(q)), "excluded_confirmed": len(excl),
        "batch_counts": {int(k): int(v) for k, v in q.batch.value_counts().sort_index().items()},
        "month_ends": [str(month_ends[0].date()), str(month_ends[-1].date()), len(month_ends)],
        "rule": "어느 달이든 미검토 회사가 (제외 확정 회사를 뺀) 순위 40위 안이면 검토 대상. 제외가 확정될 때마다 재실행"}
json.dump(meta, open(os.path.join(CLS, "review_queue_meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(meta, ensure_ascii=False))
