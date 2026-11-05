import sys, numpy as np, pandas as pd
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portfolio import Plan, simulate, last_trade_index
d = pd.bdate_range("2020-01-01", periods=8)
R = pd.DataFrame({"a": [np.nan, .10, .10, .10, .10, .10, .10, .10], "b": [np.nan, 0, 0, 0, 0, 0, 0, 0],
                  "c": [np.nan, .5, .5, np.nan, np.nan, np.nan, np.nan, np.nan]}, index=d)  # c 는 2번째 날 이후 상폐
li = last_trade_index(R); print("last", li)
p = Plan(len(d)); p.rebalance(0, ["a", "b", "c"])
out = simulate(R, li, 0.0, p, cost_side=0.01)
print("net", np.round(out["net"], 4)); print("turn", np.round(out["turnover"], 3)); print("inv", np.round(out["invested"], 3))
# 기대: t0 결정 → t1 종가 체결(비중 1/3씩, 비용 1%) → t2 수익 = (0.1+0+0.5)/3 = 20% → c 는 t2 마지막 거래일 → 청산(비용) → 이후 a,b 와 현금
