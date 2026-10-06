"""
종목별 지표 계산. 데이터 소스와 무관 (DataFrame[open, high, low, close, volume] 만 받음).

추세 상태 정의
  ON  조건: 종가 > 20MA  AND  20MA > 5거래일 전 20MA  AND  20일 수익률 > 0
  OFF 조건: 종가 < 20MA 가 2거래일 연속
            OR (20MA < 5거래일 전 20MA  AND  20일 수익률 < 0)
  신규전환: OFF 상태가 최소 10거래일 유지된 뒤 ON 으로 바뀐 날만 인정
            (20MA 근처에서 왔다갔다하는 잡신호를 새 전환으로 세지 않기 위함)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def trend_state_machine(close, ma, slope, ret20, valid, p):
    n = len(close)
    trend = np.zeros(n, dtype=bool)
    new_turn = np.zeros(n, dtype=bool)
    turn_on = np.zeros(n, dtype=bool)

    state = False
    off_run = 0
    below_run = 0
    for i in range(n):
        if not valid[i]:
            trend[i] = state
            continue
        below_run = below_run + 1 if close[i] < ma[i] else 0
        on_cond = close[i] > ma[i] and slope[i] > 0 and ret20[i] > 0
        off_cond = below_run >= p["off_close_below_days"] or (slope[i] < 0 and ret20[i] < 0)
        if not state:
            if on_cond:
                state = True
                turn_on[i] = True
                new_turn[i] = off_run >= p["min_off_days_for_new_turn"]
                off_run = 0
            else:
                off_run += 1
        else:
            if off_cond:
                state = False
                off_run = 1
        trend[i] = state
    return trend, new_turn, turn_on


def compute_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    c = df["close"]
    v = df["volume"]
    ma = c.rolling(p["ma"]).mean()
    ma_prev = ma.shift(p["slope_lag"])
    slope = ma / ma_prev - 1
    ret20 = c / c.shift(p["ret_short"]) - 1
    ret65 = c / c.shift(p["ret_long"]) - 1
    hi = c.rolling(p["high_lookback"], min_periods=60).max()
    dist52 = c / hi - 1
    vol_avg = v.shift(1).rolling(p["vol_lookback"]).mean()
    vol_ratio = v / vol_avg
    daily_ret = c.pct_change()

    valid = c.notna() & ma.notna() & slope.notna() & ret20.notna() & (v > 0)

    trend, new_turn, turn_on = trend_state_machine(
        c.to_numpy(), ma.to_numpy(), slope.to_numpy(), ret20.to_numpy(), valid.to_numpy(), p
    )

    out = pd.DataFrame({
        "close": c, "ma20": ma, "ma20_slope5": slope,
        "ret20": ret20, "ret65": ret65, "dist52": dist52,
        "vol_ratio": vol_ratio, "daily_ret": daily_ret,
        "valid": valid, "trend": trend, "new_turn": new_turn, "turn_on": turn_on,
    }, index=df.index)
    return out
