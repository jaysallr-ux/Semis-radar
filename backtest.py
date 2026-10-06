"""
백테스트: 같은 판정 엔진을 과거 일봉 전체에 돌려서
  1) 알려진 사이클 기준점 근처에서 신호가 언제 떴는지 (탐지 지연/선행)
  2) 기준점과 무관한 구간에서 초입 신호가 몇 번 떴는지 (헛방)
  3) 신호 이후 동일가중 지수(ew_index) 20/60/120거래일 수익률
을 뽑는다.

목적은 전략 수익률이 아니라 '폭 신호가 사이클 초입 근처에서 작동했는지' 검증.
지금 살아남은 종목만 쓰므로 생존편향이 있다는 점을 감안할 것.

  python backtest.py --provider naver
  python backtest.py --provider naver --grid
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from data_provider import check_coverage, get_provider
from regime import prepare, run_engine

HERE = os.path.dirname(os.path.abspath(__file__))

# 월봉 분석에서 SK하이닉스가 사이클 시작가 대비 +30%를 처음 넘긴 시점 (2025년은 주봉 기준)
REFERENCES = ["2016-09-01", "2019-12-02", "2023-05-02", "2025-06-09"]
WIN_BEFORE, WIN_AFTER = 120, 120  # 기준점 앞뒤 거래일 창


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def entries(days, phases=("EARLY", "CONFIRMED")):
    """NORMAL/HOLD → EARLY 또는 CONFIRMED 로 진입한 인덱스"""
    idx = []
    for i in range(1, len(days)):
        if days[i]["phase"] in phases and days[i - 1]["phase"] not in ("EARLY", "CONFIRMED"):
            idx.append(i)
    return idx


def first_entry(days, phase, lo, hi):
    for i in range(max(lo, 1), min(hi, len(days))):
        if days[i]["phase"] == phase and days[i - 1]["phase"] != phase:
            return i
    return None


def evaluate(prep, days):
    """
    탐지 판정은 '기준점 ± 창 안에서 실제로 평시→초입/확인 전환이 일어났는가'만 인정한다.
    창이 열리기 전부터 켜져 있던 신호는 성공으로 세지 않고, 그 신호가 실제로 켜진 날짜를 역추적해
    '창 밖 선행(너무 이름)'으로 따로 표시한다.
    """
    dates = prep["dates"]
    bidx = prep["ew_index"]  # 일별 동일가중 누적지수
    ref_idx = [int(np.searchsorted(dates, pd.Timestamp(r))) for r in REFERENCES]
    ent = entries(days)

    ref_rows = []
    for r, ri in zip(REFERENCES, ref_idx):
        lo, hi = ri - WIN_BEFORE, ri + WIN_AFTER
        inside = [i for i in ent if lo <= i <= hi]
        c = first_entry(days, "CONFIRMED", lo, hi + 1)
        row = {"reference": r, "status": None, "entry_date": None, "entry_lag_td": None, "entry_phase": None,
               "confirm_date": dates[c].date().isoformat() if c is not None else None,
               "confirm_lag_td": (c - ri) if c is not None else None}
        if inside:
            i = inside[0]
            row.update(status="탐지", entry_date=dates[i].date().isoformat(), entry_lag_td=i - ri,
                       entry_phase=days[i]["phase"])
        else:
            before = [i for i in ent if i < lo]
            still_on = lo >= 0 and days[max(lo, 0)]["phase"] in ("EARLY", "CONFIRMED")
            if before and still_on:
                i = before[-1]
                row.update(status="창 밖 선행(너무 이름)", entry_date=dates[i].date().isoformat(),
                           entry_lag_td=i - ri, entry_phase=days[i]["phase"])
            else:
                row["status"] = "미탐지"
        ref_rows.append(row)

    false_pos, fwd = [], []
    for i in ent:
        near = any(ri - WIN_BEFORE <= i <= ri + WIN_AFTER for ri in ref_idx)
        if not near:
            false_pos.append(dates[i].date().isoformat())
        row = {"date": dates[i].date().isoformat(), "phase": days[i]["phase"], "near_reference": near}
        for k in (20, 60, 120):
            row[f"ew_fwd{k}_pct"] = round((bidx[i + k] / bidx[i] - 1) * 100, 1) if i + k < len(bidx) else None
        fwd.append(row)

    detected = sum(1 for r in ref_rows if r["status"] == "탐지")
    lags = [r["entry_lag_td"] for r in ref_rows if r["status"] == "탐지"]
    return {
        "references": ref_rows,
        "detected": detected,
        "too_early": sum(1 for r in ref_rows if r["status"].startswith("창 밖")),
        "avg_entry_lag_td": round(float(np.mean(lags)), 1) if lags else None,
        "n_entries": len(ent),
        "false_positives": false_pos,
        "entries_fwd": fwd,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="naver")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--grid", action="store_true")
    args = ap.parse_args()

    universe = load(os.path.join(HERE, "universe.json"))
    p = load(os.path.join(HERE, "params.json"))
    prov = get_provider(args.provider)
    start = dt.date.fromisoformat(args.start)
    end = dt.date.today()

    prices = {}
    for s in universe["stocks"]:
        try:
            prices[s["ticker"]] = prov.get_stock_ohlcv(s["ticker"], start, end)
            print(f"  {s['name']}: {len(prices[s['ticker']])}일")
        except Exception as e:
            print(f"  {s['name']}: 실패 {e}")
    kospi = prov.get_index_ohlcv(universe["benchmark"]["index"], start, end)

    bm = universe["benchmark"]
    ok, bad, report = check_coverage(prices, [bm["hynix"], bm["samsung"]], start)
    print(f"\n요청 시작일 {start}")
    names = {s["ticker"]: s["name"] for s in universe["stocks"]}
    for t, first in report.items():
        print(f"  {names.get(t, t)}: 첫 일봉 {first}")
    if not ok:
        raise SystemExit("[중단] 기준 종목 과거 데이터가 잘려 있음 → 백테스트 결과 신뢰 불가: " + " / ".join(bad))

    outdir = os.path.join(HERE, "backtest")
    os.makedirs(outdir, exist_ok=True)

    prep = prepare(universe, prices, kospi, p)
    days = run_engine(prep, p, memory_down=None)
    pd.DataFrame([{k: d.get(k) for k in ["date", "phase", "hynix_lead", "part_leader", "late_level",
                                         "breadth_early", "parts_active", "on_share", "vol_share"]}
                  for d in days]).to_csv(os.path.join(outdir, "daily_states.csv"), index=False, encoding="utf-8-sig")

    res = evaluate(prep, days)
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    print("\n=== 기준점 대비 탐지 (lag: 음수=선행, 거래일) ===")
    for r in res["references"]:
        print(f"  {r['reference']}  [{r['status']}]  진입 {r['entry_date']} ({r['entry_lag_td']}, {r['entry_phase']})  "
              f"확인 {r['confirm_date']} ({r['confirm_lag_td']})")
    print(f"  탐지 {res['detected']}/{len(REFERENCES)}, 창 밖 선행 {res['too_early']}, "
          f"평균 진입 지연 {res['avg_entry_lag_td']}거래일")
    print(f"  전체 진입 {res['n_entries']}회, 헛방 {len(res['false_positives'])}회: {res['false_positives']}")

    if args.grid:
        rows = []
        for ebr in [0.15, 0.20, 0.25, 0.30, 0.35]:
            for w in [5, 10, 15, 20]:
                q = copy.deepcopy(p)
                q["early_enter_breadth"], q["new_turn_window"] = ebr, w
                r = evaluate(prep, run_engine(prep, q))
                rows.append({"early_enter_breadth": ebr, "new_turn_window": w,
                             "detected": r["detected"], "too_early": r["too_early"],
                             "avg_entry_lag_td": r["avg_entry_lag_td"],
                             "entries": r["n_entries"], "false_pos": len(r["false_positives"])})
        g = pd.DataFrame(rows)
        g.to_csv(os.path.join(outdir, "grid.csv"), index=False, encoding="utf-8-sig")
        print("\n=== 임계값 그리드 ===")
        print(g.to_string(index=False))


if __name__ == "__main__":
    main()
