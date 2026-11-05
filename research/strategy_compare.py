"""
v3 1단계: 전략 비교 백테스트. 규칙은 research/PREREG_v3_step1.md 가 authority.

  python research/strategy_compare.py --provider naver
  python research/strategy_compare.py --provider fake   # 파이프라인 확인용

출력: research/results/ (summary.json, metrics.csv, yearly.csv, monthly_regime.csv, equity.csv, report.txt)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data_provider import check_coverage, get_provider  # noqa: E402
from regime import prepare, run_engine, subject_index  # noqa: E402

# ---------------- 사전등록 상수 (PREREG_v3_step1.md) ----------------
DATA_START = "2015-01-01"
EVAL_START = "2016-01-04"
COST_SIDE = 0.0025
COST_SIDE_SENS = 0.0050
CASH_ANNUAL = 0.025
LAG = 2                      # t 종가 신호 → t+1 종가 체결 → t+2 수익률부터
MA_B = 60
MOM_D = 126
MOM_D63 = 63
PART_MIN_VALID = 2
BOOT_BLOCK = 63
BOOT_N = 2000
BOOT_SEED = 42
WF_FIRST_YEAR = 2019
WF_TRAIL_YEARS = 3
CANDIDATES = ["A", "B", "C", "D", "D+B"]
AUX = ["C2", "D63", "HYNIX"]
NAMES = {
    "A": "A 단순 보유", "B": "B 60일선", "C": "C 단계 판정(사이클확인)", "D": "D RS 1위 파트(126일)",
    "D+B": "D+B 60일선 필터", "C2": "보조 C2 초입+확인", "D63": "보조 D63(63일)", "HYNIX": "보조 하닉 단순 보유",
}
ALL_PARTS = ["대형", "전공정", "후공정", "기판", "소재", "테스트", "부품"]
CASH = "cash"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 신호 → 보유 라벨 ----------------
def month_end_mask(dates: pd.DatetimeIndex) -> np.ndarray:
    m = np.zeros(len(dates), dtype=bool)
    per = dates.to_period("M")
    m[:-1] = per[:-1] != per[1:]
    m[-1] = False  # 마지막 날은 월말 확정 아님
    return m


def rs_part_labels(prep, part_idx: dict, part_valid_n: dict, lookback: int) -> np.ndarray:
    """매월 마지막 거래일 종가 기준 lookback 수익률 1위 파트. 다음 월말까지 유지. 첫 판정 전은 현금"""
    dates = prep["dates"]
    T = len(dates)
    lab = np.array([CASH] * T, dtype=object)
    me = month_end_mask(dates)
    cur = CASH
    for t in range(T):
        if me[t] and t >= lookback:
            best, best_r = None, -np.inf
            for pn, idx in part_idx.items():
                if part_valid_n[pn][t] < PART_MIN_VALID or part_valid_n[pn][t - lookback] < 1:
                    continue
                r = idx[t] / idx[t - lookback] - 1
                if np.isfinite(r) and r > best_r:
                    best, best_r = pn, r
            if best is not None:
                cur = best
        lab[t] = cur
    return lab


def build_signals(prep, days) -> dict:
    T = len(prep["dates"])
    x = prep["ew_index"]
    ma = pd.Series(x).rolling(MA_B).mean().to_numpy()
    b_on = np.zeros(T, dtype=bool)
    b_on[MA_B - 1:] = x[MA_B - 1:] > ma[MA_B - 1:]
    phase = np.array([d["phase"] for d in days])

    part_idx = {pn: subject_index(prep, pn) for pn in ALL_PARTS}
    part_valid_n = {pn: (prep["valid"][:, prep["parts"] == pn]).sum(axis=1) for pn in ALL_PARTS}
    d126 = rs_part_labels(prep, part_idx, part_valid_n, MOM_D)
    d63 = rs_part_labels(prep, part_idx, part_valid_n, MOM_D63)

    basket = np.array(["basket"] * T, dtype=object)
    cash = np.array([CASH] * T, dtype=object)
    sig = {
        "A": basket.copy(),
        "B": np.where(b_on, "basket", CASH).astype(object),
        "C": np.where(phase == "CONFIRMED", "basket", CASH).astype(object),
        "D": d126,
        "D+B": np.where(b_on, d126, cash).astype(object),
        "C2": np.where(np.isin(phase, ["EARLY", "CONFIRMED"]), "basket", CASH).astype(object),
        "D63": d63,
        "HYNIX": np.array(["hynix"] * T, dtype=object),
    }
    return sig, part_idx


# ---------------- 수익률 ----------------
def asset_returns(prep, part_idx) -> dict:
    T = len(prep["dates"])
    x = prep["ew_index"]
    r = {"basket": np.r_[0.0, x[1:] / x[:-1] - 1]}
    for pn, idx in part_idx.items():
        r[pn] = np.r_[0.0, idx[1:] / idx[:-1] - 1]
    hy = prep["arr"]["close"][:, prep["idx_hynix"]]
    with np.errstate(all="ignore"):
        hr = np.r_[0.0, hy[1:] / hy[:-1] - 1]
    r["hynix"] = np.nan_to_num(hr)
    r[CASH] = np.full(T, (1 + CASH_ANNUAL) ** (1 / 252) - 1)
    return r


def simulate(sig_lab: np.ndarray, rets: dict, cost_side: float):
    """
    sig_lab[t] = t 종가 신호. 보유 hold[t] = sig_lab[t-LAG] (t일 수익률을 받는 자산).
    보유가 바뀌는 날 그 날 수익률에서 비용 차감: 현금↔자산 = 편도 1회, 자산A→자산B = 편도 2회
    """
    T = len(sig_lab)
    hold = np.array([CASH] * T, dtype=object)
    hold[LAG:] = sig_lab[:-LAG]
    gross = np.array([rets[h][t] for t, h in enumerate(hold)])
    turn = np.zeros(T)
    for t in range(1, T):
        a, b = hold[t - 1], hold[t]
        if a != b:
            turn[t] = (0 if a == CASH else 1) + (0 if b == CASH else 1)
    net = gross - turn * cost_side
    return hold, gross, net, turn


# ---------------- 지표 ----------------
def max_drawdown(eq: np.ndarray, dates):
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    ti = int(np.argmin(dd))
    pi = int(np.argmax(eq[:ti + 1])) if ti > 0 else 0
    rec = next((k for k in range(ti, len(eq)) if eq[k] >= eq[pi]), None)
    return {"mdd_pct": round(float(dd[ti]) * 100, 1),
            "peak": dates[pi].date().isoformat(), "trough": dates[ti].date().isoformat(),
            "recovered": dates[rec].date().isoformat() if rec is not None else "미회복"}


def metrics(net, gross, hold, turn, dates, rf_daily):
    n = len(net)
    yrs = n / 252
    eq = np.cumprod(1 + net)
    eqg = np.cumprod(1 + gross)
    ex = net - rf_daily
    sd = np.std(ex, ddof=1)
    return {
        "cagr_net_pct": round((eq[-1] ** (1 / yrs) - 1) * 100, 1),
        "cagr_gross_pct": round((eqg[-1] ** (1 / yrs) - 1) * 100, 1),
        "sharpe_net": round(float(np.mean(ex) / sd * np.sqrt(252)), 2) if sd > 0 else None,
        "vol_pct": round(float(np.std(net, ddof=1) * np.sqrt(252)) * 100, 1),
        **max_drawdown(eq, dates),
        "turnover_per_year": round(float(turn.sum() / yrs), 1),
        "invested_share": round(float(np.mean(hold != CASH)), 3),
        "final_multiple": round(float(eq[-1]), 2),
    }


def sharpe(x, rf):
    ex = x - rf
    sd = np.std(ex, ddof=1)
    return np.mean(ex) / sd * np.sqrt(252) if sd > 0 else np.nan


def block_bootstrap_diff(a, b, rf, rng):
    n = len(a)
    nb = int(np.ceil(n / BOOT_BLOCK))
    diffs = np.empty(BOOT_N)
    for k in range(BOOT_N):
        starts = rng.integers(0, n - BOOT_BLOCK + 1, size=nb)
        idx = (starts[:, None] + np.arange(BOOT_BLOCK)[None, :]).ravel()[:n]
        diffs[k] = sharpe(a[idx], rf) - sharpe(b[idx], rf)
    obs = sharpe(a, rf) - sharpe(b, rf)
    return {"sharpe_diff": round(float(obs), 2), "p_gt0": round(float(np.mean(diffs > 0)), 3),
            "ci90": [round(float(np.percentile(diffs, 5)), 2), round(float(np.percentile(diffs, 95)), 2)]}


# ---------------- 메인 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="naver")
    ap.add_argument("--outdir", default=os.path.join(ROOT, "research", "results"))
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    universe = load(os.path.join(ROOT, "universe.json"))
    p = load(os.path.join(ROOT, "params.json"))
    prov = get_provider(args.provider)
    start = dt.date.fromisoformat(DATA_START)
    end = dt.date.today()
    prices = {s["ticker"]: prov.get_stock_ohlcv(s["ticker"], start, end) for s in universe["stocks"]}
    kospi = prov.get_index_ohlcv(universe["benchmark"]["index"], start, end)
    ok, bad, _, _ = check_coverage(prices, universe, start, require_all=True)
    if not ok:
        raise SystemExit("[중단] 커버리지 문제:\n  " + "\n  ".join(bad))

    prep = prepare(universe, prices, kospi, p, start=start)
    days = run_engine(prep, p)
    dates = prep["dates"]
    sig, part_idx = build_signals(prep, days)
    rets = asset_returns(prep, part_idx)

    e0 = int(np.searchsorted(dates, pd.Timestamp(EVAL_START)))
    ed = dates[e0:]
    rf = rets[CASH][e0:]

    res, series, holds = {}, {}, {}
    for key in CANDIDATES + AUX:
        hold, gross, net, turn = simulate(sig[key], rets, COST_SIDE)
        _, _, net_s, _ = simulate(sig[key], rets, COST_SIDE_SENS)
        sl = slice(e0, None)
        m = metrics(net[sl], gross[sl], hold[sl], turn[sl], ed, rf)
        m["cagr_net_cost050_pct"] = round((np.prod(1 + net_s[sl]) ** (252 / len(ed)) - 1) * 100, 1)
        res[key] = m
        series[key] = net[sl]
        holds[key] = hold[sl]

    # 연도별 / 시기별
    yr = pd.Series(ed.year, index=ed)
    yearly = pd.DataFrame({k: pd.Series(series[k], index=ed).groupby(yr).apply(lambda s: np.prod(1 + s) - 1) * 100
                           for k in series}).round(1)
    eras = {"2016~2018": (2016, 2018), "2019~2022": (2019, 2022), "2023~끝": (2023, 2100)}
    era_tab = {}
    for en, (a, b) in eras.items():
        m = (ed.year >= a) & (ed.year <= b)
        era_tab[en] = {k: {"cagr_pct": round(float(np.prod(1 + series[k][m]) ** (252 / m.sum()) - 1) * 100, 1),
                           "sharpe": round(float(sharpe(series[k][m], rf[m])), 2),
                           "mdd_pct": max_drawdown(np.cumprod(1 + series[k][m]), ed[m])["mdd_pct"]}
                       for k in series}

    # 월별 구간 (바스켓 월수익률 기준)
    mon = ed.to_period("M")
    mret = pd.DataFrame({k: pd.Series(series[k], index=ed).groupby(mon).apply(lambda s: np.prod(1 + s) - 1)
                         for k in series})
    base_m = pd.Series(rets["basket"][e0:], index=ed).groupby(mon).apply(lambda s: np.prod(1 + s) - 1)
    label = np.where(base_m > 0.05, "상승월", np.where(base_m < -0.05, "하락월", "횡보월"))
    regime = (mret.groupby(label).mean() * 100).round(2)
    regime["개월수"] = pd.Series(label).value_counts()

    # 부트스트랩
    rng = np.random.default_rng(BOOT_SEED)
    boot = {}
    for k in CANDIDATES + AUX:
        for ref in ("B", "A"):
            if k == ref:
                continue
            boot[f"{k} vs {ref}"] = block_bootstrap_diff(series[k], series[ref], rf, rng)

    # 선택 walk-forward
    wf_rows, wf_net = [], []
    years = sorted(set(ed.year))
    for y in years:
        if y < WF_FIRST_YEAR:
            continue
        trail = (ed.year >= y - WF_TRAIL_YEARS) & (ed.year <= y - 1)
        cur = ed.year == y
        pick = max(CANDIDATES, key=lambda k: sharpe(series[k][trail], rf[trail]))
        wf_net.append(series[pick][cur])
        wf_rows.append({"year": y, "picked": pick,
                        "picked_ret_pct": round((np.prod(1 + series[pick][cur]) - 1) * 100, 1),
                        "B_ret_pct": round((np.prod(1 + series["B"][cur]) - 1) * 100, 1)})
    wf_net = np.concatenate(wf_net)
    wmask = ed.year >= WF_FIRST_YEAR
    wf_summary = {
        "selected": {"cagr_pct": round(float(np.prod(1 + wf_net) ** (252 / len(wf_net)) - 1) * 100, 1),
                     "sharpe": round(float(sharpe(wf_net, rf[wmask])), 2),
                     "mdd_pct": max_drawdown(np.cumprod(1 + wf_net), ed[wmask])["mdd_pct"]},
        "B_same_period": {"cagr_pct": round(float(np.prod(1 + series["B"][wmask]) ** (252 / wmask.sum()) - 1) * 100, 1),
                          "sharpe": round(float(sharpe(series["B"][wmask], rf[wmask])), 2),
                          "mdd_pct": max_drawdown(np.cumprod(1 + series["B"][wmask]), ed[wmask])["mdd_pct"]},
        "years": wf_rows,
    }

    # 판정 (사전등록 기준)
    verdict = {}
    for k in ["A", "C", "D", "D+B"]:
        bt = boot[f"{k} vs B"]
        mdd_ok = res[k]["mdd_pct"] >= res["B"]["mdd_pct"] - 5
        verdict[k] = {"beats_B": bool(bt["p_gt0"] >= 0.90 and mdd_ok), "p_gt0": bt["p_gt0"], "mdd_ok": bool(mdd_ok)}

    # D 보유 파트 분포
    d_mix = {k: pd.Series(holds[k]).value_counts(normalize=True).round(3).to_dict() for k in ["D", "D+B", "D63"]}

    # 저장
    pd.DataFrame(res).T.to_csv(os.path.join(args.outdir, "metrics.csv"), encoding="utf-8-sig")
    yearly.to_csv(os.path.join(args.outdir, "yearly.csv"), encoding="utf-8-sig")
    regime.to_csv(os.path.join(args.outdir, "monthly_regime.csv"), encoding="utf-8-sig")
    eq = pd.DataFrame({k: np.cumprod(1 + series[k]) for k in series}, index=ed)
    eq["D_hold"], eq["DB_hold"] = holds["D"], holds["D+B"]
    eq.to_csv(os.path.join(args.outdir, "equity.csv"), encoding="utf-8-sig")
    summary = {"prereg": "research/PREREG_v3_step1.md", "provider": prov.name,
               "eval_start": ed[0].date().isoformat(), "eval_end": ed[-1].date().isoformat(),
               "n_days": len(ed), "metrics": res, "eras": era_tab, "bootstrap_sharpe_diff": boot,
               "walk_forward_selection": wf_summary, "verdict_vs_B": verdict, "d_holdings_share": d_mix}
    with open(os.path.join(args.outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 콘솔 리포트
    print(f"평가 {summary['eval_start']} ~ {summary['eval_end']} ({len(ed)}거래일), provider={prov.name}")
    print("\n==== 전략별 성과 (비용 편도 0.25% 차감) ====")
    cols = ["cagr_net_pct", "cagr_gross_pct", "cagr_net_cost050_pct", "sharpe_net", "vol_pct", "mdd_pct",
            "peak", "trough", "recovered", "turnover_per_year", "invested_share", "final_multiple"]
    t = pd.DataFrame(res).T[cols]
    t.index = [NAMES[k] for k in t.index]
    with pd.option_context("display.width", 250, "display.max_columns", 20):
        print(t.to_string())
        print("\n==== 연도별 수익률 % ====")
        print(yearly.to_string())
        print("\n==== 시기별 ====")
        for en, row in era_tab.items():
            print(f"  {en}: " + " / ".join(f"{k} {v['cagr_pct']}%·샤프{v['sharpe']}·MDD{v['mdd_pct']}%"
                                         for k, v in row.items()))
        print("\n==== 월 구간별 평균 월수익률 % (바스켓 월 ±5% 기준) ====")
        print(regime.to_string())
    print("\n==== 샤프 차이 블록 부트스트랩 (63일 블록, 2000회) ====")
    for k, v in boot.items():
        print(f"  {k}: 차이 {v['sharpe_diff']}, P(>0)={v['p_gt0']}, 90%구간 {v['ci90']}")
    print("\n==== 선택 walk-forward (직전 3년 샤프 1등을 다음 해 보유) ====")
    for r in wf_rows:
        print(f"  {r['year']}: {r['picked']} {r['picked_ret_pct']}% (B {r['B_ret_pct']}%)")
    print(f"  선택 전략: {wf_summary['selected']} / 같은 기간 B: {wf_summary['B_same_period']}")
    print("\n==== 사전등록 판정: B보다 낫다 = P(샤프차>0)>=0.90 AND MDD가 B-5%p 이내 ====")
    for k, v in verdict.items():
        print(f"  {k}: {'통과' if v['beats_B'] else '불통과'} (P={v['p_gt0']}, MDD조건 {v['mdd_ok']})")
    print("\n==== D 보유 파트 비중 ====")
    for k, v in d_mix.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        main()
