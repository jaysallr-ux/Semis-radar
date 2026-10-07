"""Frozen pullback-pause research collector/backtest.

Historical output is SMOKE TEST ONLY, never a pass/fail test.
The actual OOS policy starts 2026-10-08 and is defined in oos_evaluation_policy.json.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from data_provider import check_coverage, get_provider
from regime import prepare, run_engine

HERE = Path(__file__).resolve().parent
OUT = HERE / "backtest_pullback_pause"
START = dt.date(2015, 1, 1)
END = dt.date.today()
COOLDOWN = 20
WINDOWS = (20, 60, 120)


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def late_is_warning(level):
    return level in ("후반경고", "강한 후반경고")


def pause_states(days):
    """Frozen state machine: pause on OR; resume only CONFIRMED + leader exists."""
    paused = False
    out = []
    for d in days:
        if paused:
            if d.get("phase") == "CONFIRMED" and d.get("part_leader"):
                paused = False
        else:
            if (d.get("phase") != "CONFIRMED" or not d.get("part_leader") or late_is_warning(d.get("late_level"))):
                paused = True
        out.append(paused)
    return out


def path_stats(close, i, k):
    if i + k >= len(close) or not np.isfinite(close[i]) or close[i] <= 0:
        return None, None, None
    seg = close[i + 1:i + k + 1] / close[i] - 1
    seg = seg[np.isfinite(seg)]
    if len(seg) < k:
        return None, None, None
    return float(seg[-1]), float(seg.min()), float(seg.max())


def collect(prep, days, universe):
    dates = prep["dates"]
    close = prep["arr"]["close"]
    ma20 = prep["arr"]["ma20"]
    trend = prep["trend"]
    valid = prep["valid"]
    parts = prep["parts"]
    tickers = prep["tickers"]
    names = prep["names"]
    paused = pause_states(days)
    last_event = {t: -10_000 for t in tickers}
    rows = []

    # Strictly prior 20 trading days: no same-day look-ahead in the reference high.
    for i in range(20, len(dates)):
        leader = days[i].get("part_leader")
        for j, ticker in enumerate(tickers):
            if not valid[i, j] or not np.isfinite(close[i, j]) or not np.isfinite(ma20[i, j]):
                continue
            eligible = (leader is not None and parts[j] == leader) or bool(trend[i, j])
            if not eligible or i - last_event[ticker] < COOLDOWN:
                continue
            hist = close[i - 20:i, j]
            hist = hist[np.isfinite(hist)]
            if len(hist) < 20:
                continue
            prior_high = float(np.max(hist))
            drawdown = float(close[i, j] / prior_high - 1)
            ma_dist = float(close[i, j] / ma20[i, j] - 1)
            if drawdown > -0.08 or ma_dist < -0.03 or ma_dist > 0.01:
                continue
            r = {
                "i": i, "date": dates[i].date().isoformat(), "ticker": ticker, "name": str(names[j]),
                "part": str(parts[j]), "phase": days[i].get("phase"), "part_leader": leader,
                "late_level": days[i].get("late_level"), "bucket": "paused" if paused[i] else "normal",
                "entry_close": float(close[i, j]), "drawdown20": drawdown, "ma20_dist": ma_dist,
            }
            for k in WINDOWS:
                f, mae, mfe = path_stats(close[:, j], i, k)
                r[f"fwd{k}"] = f; r[f"mae{k}"] = mae; r[f"mfe{k}"] = mfe
            rows.append(r)
            last_event[ticker] = i
    return rows


def summarize(rows):
    result = {}
    for bucket in ("paused", "normal"):
        rr = [r for r in rows if r["bucket"] == bucket]
        b = {"n_all": len(rr)}
        for k in WINDOWS:
            mature = [r for r in rr if r[f"fwd{k}"] is not None]
            b[str(k)] = {
                "n": len(mature),
                "mean_forward_return": None if not mature else float(np.mean([r[f"fwd{k}"] for r in mature])),
                "median_forward_return": None if not mature else float(np.median([r[f"fwd{k}"] for r in mature])),
                "mean_MAE": None if not mature else float(np.mean([r[f"mae{k}"] for r in mature])),
                "mean_MFE": None if not mature else float(np.mean([r[f"mfe{k}"] for r in mature])),
                "loss_rate": None if not mature else float(np.mean([r[f"fwd{k}"] < 0 for r in mature])),
            }
        result[bucket] = b
    return result


def main():
    universe, params = load("universe.json"), load("params.json")
    provider = get_provider("naver")
    prices = {}
    for s in universe["stocks"]:
        prices[s["ticker"]] = provider.get_stock_ohlcv(s["ticker"], START, END)
    ok, bad, _, _ = check_coverage(prices, universe, START, require_all=True)
    if not ok:
        raise RuntimeError("coverage failure: " + "; ".join(bad))
    kospi = provider.get_index_ohlcv(universe["benchmark"]["index"], START, END)
    prep = prepare(universe, prices, kospi, params, start=START)
    days = run_engine(prep, params)
    rows = collect(prep, days, universe)
    summary = summarize(rows)
    result = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "HISTORICAL_SMOKE_ONLY_NOT_OOS_PASS_FAIL",
        "frozen_rule_reference": "oos_evaluation_policy.json/pullback_pause_oos",
        "sample_counts": {k: v["n_all"] for k, v in summary.items()},
        "summary": summary,
        "warning": "Historical data are contaminated by prior research. Do not use this result to approve or retune the rule."
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).drop(columns=["i"], errors="ignore").to_csv(OUT / "events.csv", index=False, encoding="utf-8-sig")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
