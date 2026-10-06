"""
데이터 소스 어댑터.

계산 로직(indicators.py, regime.py)은 이 파일의 DataProvider 인터페이스만 쓴다.
소스를 바꿀 때는 이 파일에 클래스 하나 추가하고 get_provider()에 등록하면 끝.

모든 provider는 같은 형식을 돌려줘야 한다:
    DatetimeIndex(오름차순) + 컬럼 [open, high, low, close, volume] (float)
    가격은 수정주가 기준.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import time
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import requests

COLS = ["open", "high", "low", "close", "volume"]


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=COLS)
    df = df[COLS].astype(float)
    df.index = pd.to_datetime(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


class DataProvider:
    name = "base"

    def get_stock_ohlcv(self, ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        raise NotImplementedError

    def get_index_ohlcv(self, code: str, start: dt.date, end: dt.date) -> pd.DataFrame | None:
        """지수(KOSPI). 못 가져오면 None. None이면 KOSPI 보조 RS는 계산에서 빠진다."""
        return None


class NaverChartProvider(DataProvider):
    """
    네이버 차트 XML 엔드포인트 직접 호출 (수정주가 일봉).
    pykrx의 adjusted=True 경로도 내부적으로 이 엔드포인트를 쓴다.
    거래대금은 안 주고 거래량만 준다.
    """
    name = "naver"
    URL = "https://fchart.stock.naver.com/sise.nhn"

    def __init__(self, sleep: float = 0.3, retries: int = 3, timeout: int = 15):
        self.sleep = sleep
        self.retries = retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (semis-radar)"})

    def _fetch(self, symbol: str, count: int) -> pd.DataFrame:
        params = {"symbol": symbol, "timeframe": "day", "count": count, "requestType": 0}
        last_err = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(self.URL, params=params, timeout=self.timeout)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                rows = []
                for item in root.iter("item"):
                    parts = item.get("data", "").split("|")
                    if len(parts) < 6:
                        continue
                    rows.append(parts[:6])
                if not rows:
                    raise ValueError(f"{symbol}: 응답에 일봉 데이터 없음")
                df = pd.DataFrame(rows, columns=["date"] + COLS)
                df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
                df = df.set_index("date")
                for c in COLS:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                time.sleep(self.sleep)
                return _clean(df)
            except Exception as e:  # 네트워크/파싱 오류 → 재시도
                last_err = e
                time.sleep(1.0 + attempt)
        raise RuntimeError(f"{symbol}: 데이터 수신 실패 ({last_err})")

    def _count(self, start: dt.date) -> int:
        # 달력일 수 >= 거래일 수 이므로 넉넉히 요청
        return (dt.date.today() - start).days + 10

    def get_stock_ohlcv(self, ticker, start, end):
        df = self._fetch(ticker, self._count(start))
        return df.loc[str(start):str(end)]

    def get_index_ohlcv(self, code, start, end):
        try:
            df = self._fetch(code, self._count(start))
            return df.loc[str(start):str(end)]
        except Exception as e:
            print(f"[경고] 지수 {code} 수신 실패 → KOSPI 보조 RS 제외: {e}")
            return None


class PykrxProvider(DataProvider):
    """pykrx 래퍼. 일봉은 adjusted=True(네이버 경로)라 KRX 로그인 없이 동작.
    지수는 KRX 경로라 KRX_ID/KRX_PW 환경변수가 없으면 실패할 수 있음."""
    name = "pykrx"

    def __init__(self, sleep: float = 0.3):
        from pykrx import stock  # 이 provider 쓸 때만 import
        self.stock = stock
        self.sleep = sleep

    def get_stock_ohlcv(self, ticker, start, end):
        df = self.stock.get_market_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker, adjusted=True)
        time.sleep(self.sleep)
        df = df.rename(columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"})
        return _clean(df)

    def get_index_ohlcv(self, code, start, end):
        try:
            df = self.stock.get_index_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1001")
            df = df.rename(columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"})
            return _clean(df)
        except Exception as e:
            print(f"[경고] pykrx 지수 수신 실패 → KOSPI 보조 RS 제외: {e}")
            return None


class KisProvider(DataProvider):
    """
    한국투자증권 KIS Open API 자리.
    구현 시 참고:
      - 국내주식 기간별시세(일봉) 조회 API, 수정주가 옵션 사용
      - 1회 호출당 반환 건수 제한이 있어 기간을 나눠 반복 호출해야 함
      - APP_KEY / APP_SECRET 은 반드시 GitHub Secrets → 환경변수로만 주입. 코드에 쓰지 말 것.
    정확한 엔드포인트/파라미터는 KIS Developers 문서 확인 후 채울 것.
    """
    name = "kis"

    def get_stock_ohlcv(self, ticker, start, end):
        raise NotImplementedError("KIS provider 미구현. KIS Developers 문서 보고 채울 것.")


class FakeProvider(DataProvider):
    """
    네트워크 없이 파이프라인 동작만 확인하는 가짜 데이터.
    공통 반도체 사이클 요인 + 종목별 베타/노이즈. 실제 시장과 무관.
    """
    name = "fake"
    ORIGIN = dt.date(2014, 1, 1)

    def __init__(self):
        self.days = pd.bdate_range(self.ORIGIN, dt.date.today())
        n = len(self.days)
        rng = np.random.default_rng(7)
        t = np.arange(n)
        cycle = 0.0012 * np.sin(2 * np.pi * t / 700.0)
        self.factor = cycle + rng.normal(0, 0.012, n)

    def _series(self, key: str, beta_scale=1.0):
        seed = int(hashlib.md5(key.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        beta = rng.uniform(0.7, 1.8) * beta_scale
        lag = int(rng.integers(0, 40))
        f = np.roll(self.factor, lag)
        f[:lag] = 0
        ret = beta * f + rng.normal(0, 0.02, len(f))
        close = 10000 * np.exp(np.cumsum(ret))
        vol = np.exp(rng.normal(12, 0.4, len(f))) * (1 + 15 * np.abs(ret))
        df = pd.DataFrame({
            "open": close * (1 + rng.normal(0, 0.003, len(f))),
            "high": close * 1.01, "low": close * 0.99, "close": close, "volume": vol,
        }, index=self.days)
        return _clean(df)

    def get_stock_ohlcv(self, ticker, start, end):
        return self._series(ticker).loc[str(start):str(end)]

    def get_index_ohlcv(self, code, start, end):
        return self._series("IDX" + code, beta_scale=0.5).loc[str(start):str(end)]


def get_provider(name: str) -> DataProvider:
    name = name.lower()
    if name == "naver":
        return NaverChartProvider()
    if name == "pykrx":
        return PykrxProvider()
    if name == "kis":
        return KisProvider()
    if name == "fake":
        return FakeProvider()
    raise ValueError(f"알 수 없는 provider: {name}")


def check_coverage(prices: dict, sentinels: list[str], start: dt.date, tolerance_days: int = 20):
    """
    조용한 실패 방지: 오래 상장된 기준 종목(하닉·삼전)의 첫 일봉이 요청 시작일 근처인지 확인.
    소스가 내부적으로 기간을 잘라버리면 여기서 걸린다.
    신규 상장 종목은 첫 날짜가 늦은 게 정상이라 판정에는 안 쓰고 리포트만 한다.
    """
    report = {t: (df.index.min().date().isoformat() if df is not None and not df.empty else None)
              for t, df in prices.items()}
    limit = start + dt.timedelta(days=tolerance_days)
    bad = []
    for t in sentinels:
        first = report.get(t)
        if first is None or dt.date.fromisoformat(first) > limit:
            bad.append(f"{t}: 요청 시작 {start}, 실제 첫 일봉 {first}")
    return len(bad) == 0, bad, report
