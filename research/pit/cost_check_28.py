"""
v3 2단계-가: 기존 28종목 유니버스에서 '내부 재조정 비용 누락' 영향만 따로 측정.
유니버스·분류는 1단계와 같고, 가격만 marcap(네이버와 대조 완료), 체결 방식만 바꾼다.

  ① 1단계 방식 : 매일 동일가중 재조정, 비용은 노출 전환·파트 교체 때만
  ② 현실 방식  : 체결일에만 동일가중으로 맞추고 월중 방치, 월말 재조정·편입교체·상폐청산 비용 전부

전략 A / B / D / D+B 규칙(126일, 월 1회, 60일선, 편도 0.25%, 현금 연 2.5%, 하루 지연)은 1단계 사전등록 그대로.
  python research/pit/cost_check_28.py --marcap /path/to/marcap
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "research"))

from marcap_data import load_marcap, returns_panel  # noqa: E402
from portfolio import Plan, last_trade_index, month_end_positions, simulate  # noqa: E402
from strategy_compare import (BOOT_SEED, CASH_ANNUAL, COST_SIDE, EVAL_START, MA_B, MOM_D,  # noqa: E402
                              PART_MIN_VALID, block_bootstrap_diff, max_drawdown, sharpe)

MIN_HIST = 20  # 편입 가능: 상장 후 20거래일 이력 (1단계 엔진 valid 와 같은 취지)


def eligible_matrix(R: pd.DataFrame, valid_from: dict) -> pd.DataFrame:
    hist = R.notna().cumsum()
    ok = (hist > MIN_HIST) & R.notna()
    for c, d in valid_from.items():
        if d and c in ok:
            ok.loc[:pd.Timestamp(d), c] = False
    return ok


def daily_ew(R, ok):
    return R.where(ok).mean(axis=1).fillna(0).to_numpy()


def run(marcap_dir, out_dir):
    u = json.load(open(os.path.join(ROOT, "universe.json"), encoding="utf-8"))
    codes = [s["ticker"] for s in u["stocks"]]
    part_of = {s["ticker"]: s["part"] for s in u["stocks"]}
    vf = {s["ticker"]: s.get("valid_from") for s in u["stocks"]}
    df = load_marcap(marcap_dir)
    R = returns_panel(df, codes)
    dates = R.index
    T = len(dates)
    ok = eligible_matrix(R, vf)
    li = last_trade_index(R)
    rf = (1 + CASH_ANNUAL) ** (1 / 252) - 1
    me = month_end_positions(dates)
    parts = list(dict.fromkeys(part_of.values()))

    def members(s, part=None):
        row = ok.iloc[s]
        return [c for c in codes if row[c] and (part is None or part_of[c] == part)]

    # ---------- ① 1단계 방식 (매일 동일가중) ----------
    basket_d = daily_ew(R, ok)
    part_d = {pn: daily_ew(R[[c for c in codes if part_of[c] == pn]], ok[[c for c in codes if part_of[c] == pn]])
              for pn in parts}

    # ---------- ② 현실 방식: 지수(모멘텀·60일선 판정용)는 비용 0 월 재조정 포트폴리오 ----------
    def monthly_index(part=None):
        pl = Plan(T)
        for s in range(T):
            if me[s] or s == 0:
                pl.rebalance(s, members(s, part))
        return simulate(R, li, 0.0, pl, 0.0)["gross"]

    basket_m = monthly_index()
    part_m = {pn: monthly_index(pn) for pn in parts}

    def b_signal(daily_ret):
        x = np.cumprod(1 + daily_ret)
        ma = pd.Series(x).rolling(MA_B).mean().to_numpy()
        on = np.zeros(T, dtype=bool)
        on[MA_B - 1:] = x[MA_B - 1:] > ma[MA_B - 1:]
        return on

    def d_choice(part_ret):
        idx = {pn: np.cumprod(1 + r) for pn, r in part_ret.items()}
        lab = [None] * T
        cur = None
        for s in range(T):
            if me[s] and s >= MOM_D:
                best, br = None, -np.inf
                for pn in parts:
                    if len(members(s, pn)) < PART_MIN_VALID:
                        continue
                    r = idx[pn][s] / idx[pn][s - MOM_D] - 1
                    if np.isfinite(r) and r > br:
                        best, br = pn, r
                if best:
                    cur = best
            lab[s] = cur
        return lab

    results, series = {}, {}

    # ① 1단계 방식 재현 (자산 = 매일 동일가중 지수, 비용은 자산 전환 때만)
    b_on_d = b_signal(basket_d)
    d_lab_d = d_choice(part_d)
    rets_d = {"basket": basket_d, **part_d, None: np.full(T, rf), "cash": np.full(T, rf)}

    def sim_label(lab):
        hold = [None] * T
        hold[2:] = lab[:-2]
        g = np.array([rets_d[h if h else "cash"][t] for t, h in enumerate(hold)])
        tv = np.zeros(T)
        for t in range(1, T):
            a, b = hold[t - 1], hold[t]
            if (a or "cash") != (b or "cash"):
                tv[t] = (0 if (a or "cash") == "cash" else 1) + (0 if (b or "cash") == "cash" else 1)
        return g - tv * COST_SIDE, g, tv

    labels1 = {
        "A": ["basket"] * T,
        "B": ["basket" if x else "cash" for x in b_on_d],
        "D": d_lab_d,
        "D+B": [d if (b and d) else "cash" for d, b in zip(d_lab_d, b_on_d)],
    }
    for k, lab in labels1.items():
        n, g, tv = sim_label(lab)
        series[f"{k}·①매일"] = (n, g, tv, np.array([0 if (h or "cash") == "cash" else 1 for h in ([None, None] + lab[:-2])]))

    # ② 현실 방식
    b_on_m = b_signal(basket_m)
    d_lab_m = d_choice(part_m)
    plans = {}
    pa = Plan(T)
    for s in range(T):
        if me[s] or s == 0:
            pa.rebalance(s, members(s))
    plans["A"] = pa
    pb = Plan(T)
    inv = False
    for s in range(T):
        if b_on_m[s] and (not inv or me[s]):
            pb.rebalance(s, members(s)); inv = True
        elif not b_on_m[s] and inv:
            pb.cash(s); inv = False
    plans["B"] = pb
    pdp = Plan(T)
    for s in range(T):
        if me[s] and d_lab_m[s]:
            pdp.rebalance(s, members(s, d_lab_m[s]))
    plans["D"] = pdp
    pdb = Plan(T)
    inv = False
    for s in range(T):
        lab = d_lab_m[s]
        if b_on_m[s] and lab and (not inv or me[s]):
            pdb.rebalance(s, members(s, lab)); inv = True
        elif (not b_on_m[s]) and inv:
            pdb.cash(s); inv = False
    plans["D+B"] = pdb
    for k, pl in plans.items():
        o = simulate(R, li, rf, pl, COST_SIDE)
        series[f"{k}·②월재조정"] = (o["net"], o["gross"], o["turnover"], o["invested"])

    # ---------- 지표 ----------
    e0 = int(np.searchsorted(dates, pd.Timestamp(EVAL_START)))
    ed = dates[e0:]
    rfa = np.full(len(ed), rf)
    yrs = len(ed) / 252
    for k, (n, g, tv, inv_) in series.items():
        n, g, tv, inv_ = n[e0:], g[e0:], tv[e0:], np.asarray(inv_)[e0:]
        eq = np.cumprod(1 + n)
        results[k] = {
            "cagr_net_pct": round((eq[-1] ** (1 / yrs) - 1) * 100, 1),
            "cagr_gross_pct": round((np.prod(1 + g) ** (1 / yrs) - 1) * 100, 1),
            "sharpe_net": round(float(sharpe(n, rfa)), 2),
            **max_drawdown(eq, ed),
            "turnover_per_year": round(float(tv.sum() / yrs), 1),
            "cost_drag_pct_per_year": round(float((np.prod(1 + g) ** (1 / yrs) - eq[-1] ** (1 / yrs)) * 100), 1),
            "invested_share": round(float(np.mean(inv_ > 0.01)), 3),
        }
    rng = np.random.default_rng(BOOT_SEED)
    boot = {}
    for k in ["A", "D", "D+B"]:
        boot[f"{k}·② vs B·②"] = block_bootstrap_diff(series[f"{k}·②월재조정"][0][e0:],
                                                      series["B·②월재조정"][0][e0:], rfa, rng)

    os.makedirs(out_dir, exist_ok=True)
    pd.DataFrame(results).T.to_csv(os.path.join(out_dir, "cost_check_28.csv"), encoding="utf-8-sig")
    with open(os.path.join(out_dir, "cost_check_28.json"), "w", encoding="utf-8") as f:
        json.dump({"eval": [ed[0].date().isoformat(), ed[-1].date().isoformat()], "metrics": results,
                   "bootstrap": boot,
                   "d_holdings_monthly": pd.Series([x for x in d_lab_m[e0:] if x]).value_counts(normalize=True)
                   .round(3).to_dict()}, f, ensure_ascii=False, indent=2)
    t = pd.DataFrame(results).T
    with pd.option_context("display.width", 250, "display.max_columns", 20):
        print(f"평가 {ed[0].date()} ~ {ed[-1].date()} ({len(ed)}거래일), 28종목, marcap 가격")
        print(t.to_string())
    for k, v in boot.items():
        print(f"  {k}: 샤프차 {v['sharpe_diff']} P(>0)={v['p_gt0']} 90%구간 {v['ci90']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--marcap", default=os.environ.get("MARCAP_DIR", "marcap"))
    ap.add_argument("--out", default=os.path.join(ROOT, "research", "results_step2"))
    a = ap.parse_args()
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        run(a.marcap, a.out)
