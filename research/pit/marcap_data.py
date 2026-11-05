"""
FinanceData/marcap 기반 시점별(point-in-time) 패널.

- 수익률: KRX 등락률(ChangesRatio)을 그대로 사용 → 액면분할·무상증자 등 기준가 조정 반영. 현금배당 미반영
- 상장 전·상폐 후: 해당 날짜 행이 없음 → NaN (그날 투자 불가)
- 상폐 종목: 마지막 거래일(정리매매 포함)까지의 수익률이 그대로 들어감. 그 뒤로는 행이 없음
  → 포트폴리오는 마지막 거래일 종가에 청산된 것으로 처리 (합병 대가·잔여재산은 별도 검증 대상)

  MARCAP_DIR 환경변수 또는 --marcap 인자로 marcap 저장소 경로 지정
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

COLS = ["Date", "Code", "Name", "Close", "ChangesRatio", "Volume", "Amount", "Marcap", "Stocks", "Market"]


def load_marcap(marcap_dir: str, years=range(2015, 2027), markets=("KOSPI", "KOSDAQ", "KOSDAQ GLOBAL")) -> pd.DataFrame:
    """KONEX 제외. 2022-11-21 신설된 KOSDAQ GLOBAL 세그먼트 포함 (빠뜨리면 우량 코스닥 종목이 통째로 빠짐)"""
    parts = []
    for y in years:
        f = os.path.join(marcap_dir, "data", f"marcap-{y}.parquet")
        d = pd.read_parquet(f, columns=COLS)
        parts.append(d[d.Market.isin(markets)])
    df = pd.concat(parts, ignore_index=True)
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values(["Date", "Code"]).reset_index(drop=True)


def panel(df: pd.DataFrame, field: str, codes=None) -> pd.DataFrame:
    d = df if codes is None else df[df.Code.isin(codes)]
    return d.pivot_table(index="Date", columns="Code", values=field, aggfunc="last")


def returns_panel(df: pd.DataFrame, codes=None) -> pd.DataFrame:
    """일간 수익률(소수). 상장 첫날은 NaN (기준가 대비 등락률이라 공모가 대비 수익은 안 셈)"""
    r = panel(df, "ChangesRatio", codes) / 100.0
    first = r.notna().idxmax()
    for c, d0 in first.items():
        r.loc[d0, c] = np.nan
    return r
