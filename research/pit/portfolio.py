"""
현실 체결 포트폴리오 시뮬레이터 (v3 2단계).

규칙
  - 신호·편입 결정: s일 종가. 체결: s+1일 종가. 수익: s+2일부터 (1단계와 동일한 하루 지연)
  - 동일가중은 '체결일에 맞추고 그다음엔 그냥 둠'. 매일 재조정 안 함 → 월중 비중은 수익률대로 흘러감
  - 매매가 생기는 경우: 월말 재조정(편입·편출 포함), 노출 on/off 전환, 상폐 청산
  - 비용: 실제 매매 금액(편도) × cost_side. 재조정 회전율, 종목 교체, 상폐 청산 전부 포함
  - 거래정지(그날 행 없음, 이후 다시 거래): 그날 수익 0, 매매 불가
  - 상장폐지(마지막 행 이후 행 없음): 마지막 거래일 종가(정리매매 포함)에 청산 → 현금
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class Plan:
    """s일 종가 결정. kind: 'rebalance'(members 동일가중) / 'cash' / None(유지)"""

    def __init__(self, T: int):
        self.kind = [None] * T
        self.members = [None] * T

    def rebalance(self, s: int, members):
        self.kind[s], self.members[s] = "rebalance", list(members)

    def cash(self, s: int):
        self.kind[s], self.members[s] = "cash", None


def simulate(R: pd.DataFrame, last_idx: dict, rf_daily: float, plan: Plan, cost_side: float):
    """
    R: 일간 수익률 (날짜 × 코드, NaN = 그날 거래 없음)
    last_idx: 코드 → 마지막 거래일 위치(상폐면 그 위치, 상장 중이면 len-1)
    반환: dict(net, gross, turnover, invested, n_hold)
    """
    codes = list(R.columns)
    col = {c: j for j, c in enumerate(codes)}
    Rv = R.to_numpy()
    T = len(R)
    w = np.zeros(len(codes))      # NAV 대비 비중
    cash = 1.0
    net = np.zeros(T)
    gross = np.zeros(T)
    turn = np.zeros(T)
    invested = np.zeros(T)
    n_hold = np.zeros(T, dtype=int)
    last_arr = np.array([last_idx.get(c, T - 1) for c in codes])
    pending = None  # (kind, members) 오늘 종가에 체결할 주문

    for t in range(T):
        # 1) 오늘 수익: 보유 비중 그대로
        r = np.nan_to_num(Rv[t], nan=0.0)
        port = float(w @ r) + cash * rf_daily
        nav_mult = 1.0 + port
        w = w * (1 + r) / nav_mult
        cash = cash * (1 + rf_daily) / nav_mult
        gross[t] = port
        cost = 0.0

        # 2) 상폐 청산: 오늘이 마지막 거래일인 보유 종목은 오늘 종가에 매도
        dead = (last_arr == t) & (w > 0) & (t < T - 1)
        if dead.any():
            sold = w[dead].sum()
            cost += sold * cost_side
            turn[t] += sold
            cash += sold
            w[dead] = 0.0

        # 3) 어제 결정한 주문을 오늘 종가에 체결
        if pending is not None:
            kind, members = pending
            target = np.zeros(len(codes))
            if kind == "rebalance":
                tradable = [m for m in members if m in col and not np.isnan(Rv[t, col[m]]) and last_arr[col[m]] > t]
                if tradable:
                    target[[col[m] for m in tradable]] = 1.0 / len(tradable)
            # 거래정지 종목은 팔 수도 살 수도 없음 → 현재 비중 유지
            halted = np.isnan(Rv[t]) & (w > 0)
            target[halted] = w[halted]
            if kind == "rebalance" and target.sum() > 0:
                free = 1.0 - w[halted].sum()
                nh = ~halted
                if target[nh].sum() > 0:
                    target[nh] = target[nh] / target[nh].sum() * free
            tw = float(np.abs(target - w).sum())
            cost += tw * cost_side
            turn[t] += tw
            w = target
            cash = max(0.0, 1.0 - w.sum())
        # 비용은 오늘 NAV에서 차감
        net[t] = (1 + port) * (1 - cost) - 1
        invested[t] = w.sum()
        n_hold[t] = int((w > 0).sum())

        # 4) 오늘 종가 결정 → 내일 종가 체결
        pending = (plan.kind[t], plan.members[t]) if plan.kind[t] is not None else None

    return {"net": net, "gross": gross, "turnover": turn, "invested": invested, "n_hold": n_hold}


def month_end_positions(dates: pd.DatetimeIndex) -> np.ndarray:
    per = dates.to_period("M")
    m = np.zeros(len(dates), dtype=bool)
    m[:-1] = per[:-1] != per[1:]
    return m


def last_trade_index(R: pd.DataFrame) -> dict:
    """코드별 마지막 거래일 위치. 데이터 끝까지 거래 중이면 len-1"""
    T = len(R)
    out = {}
    for c in R.columns:
        nz = np.where(R[c].notna().to_numpy())[0]
        out[c] = int(nz[-1]) if len(nz) else -1
    # 데이터 마지막 날까지 거래되는 종목은 상폐 아님
    for c, i in out.items():
        if i == T - 1:
            out[c] = T - 1
    return out
