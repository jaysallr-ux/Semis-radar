"""
백테스트 v3

평가를 세 층으로 나눈다.
  1) 사이클 탐지   : 사이클 근처에서 신호가 켜졌는가
  2) 진입 품질     : 그 신호를 따라 샀을 때 바로 처맞지 않았는가 (MAE/MFE, 이후 수익률)
  3) 랜덤 기준선   : 아무 날이나 찍었을 때보다 나은가
  4) 주도 판정 검증 (v2) : 주도 파트/하닉 주도 판정 뒤 실제로 상대 초과수익이 났는가, 단계별 이후 수익률

사이클 라벨은 두 벌을 같이 본다.
  - auto   : 동일가중 지수(ew_index)에서 규칙으로 자동 정의 (사람 판단 개입 없음) ← 메인
  - manual : 사람이 정한 구간 (과거 비교용)

과최적화 방지
  - QUALITY / AUTO_CYCLE 상수는 결과 보고 조정 금지. 미리 정한 값 그대로 쓴다.
  - --grid 는 하나 빼고 검증(Leave-One-Cycle-Out)을 같이 돌린다.

한계: 현재 생존 종목으로 과거를 재구성 → 생존편향. 목적은 '폭 신호가 쓸모 있었는가' 검증.

  python backtest.py --provider naver
  python backtest.py --provider naver --grid
  python backtest.py --provider naver --suggest-list-dates
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import itertools
import json
import os
import warnings

import numpy as np
import pandas as pd

from data_provider import check_coverage, get_provider
from oos_tracker import event_outcome
from regime import describe_change, prepare, run_engine, subject_index

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 고정 상수 (튜닝 금지) ----------------
QUALITY = {
    "mae_window": 60,       # 신호 후 60거래일 안에
    "mae_floor": -0.10,     # -10% 넘게 밀리면 진입 실패
    "fwd_window": 120,      # 120거래일 수익률이
    # 랜덤 기준선(모든 판정일)의 120일 수익률 중앙값 이상이어야 진입 성공
}
AUTO_CYCLE = {
    "reversal": 0.25,       # 지그재그 전환 기준: 25% 반대 움직임
    "min_gain": 0.50,       # 저점→고점 +50% 이상
    "min_td": 120,          # 120거래일 이상 지속
}
PRE_TD = 60                 # 사이클 시작 전 60거래일까지는 '선행' 허용
DETECT_AFTER_TD = 120       # 사이클 시작 후 120거래일 안에 첫 신호가 있어야 '탐지'
LATE_POS = 2 / 3            # 사이클 진행률 2/3 이후 신호는 '후반 재진입'
RANDOM_DRAWS = 5000

MANUAL_CYCLES = [
    {"name": "1차", "start": "2016-05-01", "end": "2018-06-30"},
    {"name": "2차", "start": "2019-05-01", "end": "2022-03-31"},
    {"name": "3차", "start": "2022-12-01", "end": "2024-12-31"},
    {"name": "4차", "start": "2025-04-01", "end": None},
]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 사이클 정의 ----------------
def auto_cycles(x: np.ndarray, dates) -> list[dict]:
    """지그재그로 저점·고점을 찾고, 조건을 만족하는 상승 구간만 사이클로 인정."""
    R = AUTO_CYCLE["reversal"]
    troughs, peaks = [], []
    mode, lo, hi = "down", 0, 0
    for i in range(1, len(x)):
        if mode == "down":
            if x[i] < x[lo]:
                lo = i
            elif x[i] >= x[lo] * (1 + R):
                troughs.append(lo)
                mode, hi = "up", i
        else:
            if x[i] > x[hi]:
                hi = i
            elif x[i] <= x[hi] * (1 - R):
                peaks.append(hi)
                mode, lo = "down", i
    ongoing = mode == "up"
    if ongoing:
        peaks.append(hi)

    cycles = []
    for k, tr in enumerate(troughs):
        pk = peaks[k] if k < len(peaks) else None
        if pk is None:
            continue
        gain = x[pk] / x[tr] - 1
        if gain >= AUTO_CYCLE["min_gain"] and pk - tr >= AUTO_CYCLE["min_td"]:
            cycles.append({"name": f"A{len(cycles) + 1}", "start_i": tr, "end_i": pk,
                           "start": dates[tr].date().isoformat(), "end": dates[pk].date().isoformat(),
                           "gain_pct": round(gain * 100, 1),
                           "ongoing": bool(ongoing and k == len(troughs) - 1)})
    return cycles


def manual_cycles(dates) -> list[dict]:
    out = []
    for c in MANUAL_CYCLES:
        si = int(np.searchsorted(dates, pd.Timestamp(c["start"])))
        ei = len(dates) - 1 if c["end"] is None else \
            min(len(dates) - 1, int(np.searchsorted(dates, pd.Timestamp(c["end"]), side="right")) - 1)
        out.append({"name": c["name"], "start_i": si, "end_i": ei, "start": c["start"],
                    "end": c["end"], "ongoing": c["end"] is None})
    return out


# ---------------- 진입 품질 ----------------
def path_stats(x: np.ndarray, i: int, k: int):
    """i일 종가 기준 k거래일 동안의 수익률, 최대낙폭(MAE), 최대상승(MFE). 데이터 부족이면 None."""
    if i + k >= len(x):
        return None, None, None
    seg = x[i + 1:i + k + 1] / x[i] - 1
    return float(seg[-1]), float(seg.min()), float(seg.max())


def entries(days):
    """평시/보류 → 초입/확인으로 진입한 날"""
    return [i for i in range(1, len(days))
            if days[i]["phase"] in ("EARLY", "CONFIRMED") and days[i - 1]["phase"] not in ("EARLY", "CONFIRMED")]


def baseline(x, days):
    """랜덤 기준선: 판정 가능한 모든 날에 샀다고 가정"""
    W, M = QUALITY["fwd_window"], QUALITY["mae_window"]
    pool = [i for i in range(len(days)) if days[i]["phase"] != "HOLD" and i + W < len(x)]
    f = np.array([path_stats(x, i, W)[0] for i in pool])
    mae = np.array([path_stats(x, i, M)[1] for i in pool])
    return pool, f, mae


def label_entries(x, days, cycles, fwd_median):
    W, M = QUALITY["fwd_window"], QUALITY["mae_window"]
    rows, seen = [], set()
    for i in entries(days):
        r = {"i": i, "date": None, "phase": days[i]["phase"], "late_level": days[i].get("late_level")}
        for k in (20, 60, 120):
            f, mae, mfe = path_stats(x, i, k)
            r[f"fwd{k}"] = None if f is None else round(f * 100, 1)
            r[f"mae{k}"] = None if mae is None else round(mae * 100, 1)
            r[f"mfe{k}"] = None if mfe is None else round(mfe * 100, 1)
        fq, _, _ = path_stats(x, i, W)
        _, mq, _ = path_stats(x, i, M)
        quality = None if (fq is None or mq is None) else bool(mq >= QUALITY["mae_floor"] and fq >= fwd_median)
        r["quality_pass"] = quality

        cyc = next((c for c in cycles if c["start_i"] - PRE_TD <= i <= c["end_i"]), None)
        if cyc is None:
            r.update(cycle=None, position=None, label="헛방", first_in_cycle=False)
        else:
            span = max(1, cyc["end_i"] - cyc["start_i"])
            pos = (i - cyc["start_i"]) / span
            r.update(cycle=cyc["name"], position=round(pos, 2), first_in_cycle=cyc["name"] not in seen)
            seen.add(cyc["name"])
            if quality is None:
                r["label"] = "판정대기(기간부족)"
            elif pos >= LATE_POS and not cyc["ongoing"]:
                r["label"] = "후반 재진입"
            elif pos < 0:
                r["label"] = "선행 진입 성공" if quality else "너무 이름"
            else:
                r["label"] = "진입 성공" if quality else "진입 실패"
        rows.append(r)
    return rows


def cycle_table(rows, cycles):
    out = []
    for c in cycles:
        win = [r for r in rows if c["start_i"] - PRE_TD <= r["i"] <= c["start_i"] + DETECT_AFTER_TD]
        first = win[0] if win else None
        usable = next((r for r in win if r["quality_pass"]), None)
        out.append({
            "cycle": c["name"], "start": c["start"], "end": c["end"],
            "detected": first is not None,
            "first_date": first["date"] if first else None,
            "first_lag_td": (first["i"] - c["start_i"]) if first else None,
            "first_label": first["label"] if first else None,
            "first_usable_date": usable["date"] if usable else None,
            "first_usable_lag_td": (usable["i"] - c["start_i"]) if usable else None,
        })
    return out


def random_test(x, pool, rows, cycles, fwd_all, rng):
    """같은 개수의 랜덤 날짜와 비교: 사이클 안 비율, 평균 120일 수익률의 백분위"""
    n = len([r for r in rows if r["fwd120"] is not None])
    if n == 0:
        return {}
    allowed = np.zeros(len(x), dtype=bool)
    for c in cycles:
        allowed[max(0, c["start_i"] - PRE_TD):c["end_i"] + 1] = True
    pool = np.array(pool)
    act_in = np.mean([allowed[r["i"]] for r in rows])
    act_f = np.mean([r["fwd120"] for r in rows if r["fwd120"] is not None]) / 100
    sims_in, sims_f = [], []
    for _ in range(RANDOM_DRAWS):
        pick = rng.choice(len(pool), size=n, replace=False)
        sims_in.append(allowed[pool[pick]].mean())
        sims_f.append(fwd_all[pick].mean())
    return {
        "signal_in_cycle_share": round(float(act_in), 3),
        "random_in_cycle_share_mean": round(float(np.mean(sims_in)), 3),
        "in_cycle_percentile": round(float(np.mean(np.array(sims_in) < act_in)) * 100, 1),
        "signal_fwd120_mean_pct": round(act_f * 100, 1),
        "random_fwd120_mean_pct": round(float(np.mean(sims_f)) * 100, 1),
        "fwd120_percentile": round(float(np.mean(np.array(sims_f) < act_f)) * 100, 1),
    }


def evaluate(prep, days, cycles, rng=None):
    x = prep["ew_index"]
    dates = prep["dates"]
    pool, fwd_all, mae_all = baseline(x, days)
    fwd_median = float(np.median(fwd_all))
    rows = label_entries(x, days, cycles, fwd_median)
    for r in rows:
        r["date"] = dates[r["i"]].date().isoformat()
    labels = pd.Series([r["label"] for r in rows]).value_counts().to_dict()
    judged = [r for r in rows if r["quality_pass"] is not None]
    res = {
        "baseline": {
            "n_days": len(pool),
            "fwd120_median_pct": round(fwd_median * 100, 1),
            "fwd120_mean_pct": round(float(np.mean(fwd_all)) * 100, 1),
            "mae60_mean_pct": round(float(np.mean(mae_all)) * 100, 1),
            "quality_pass_rate": round(float(np.mean((mae_all >= QUALITY["mae_floor"]) & (fwd_all >= fwd_median))), 3),
        },
        "signals": {
            "n": len(rows),
            "quality_pass_rate": round(float(np.mean([r["quality_pass"] for r in judged])), 3) if judged else None,
            "labels": labels,
        },
        "cycles": cycle_table(rows, cycles),
        "entries": rows,
    }
    if rng is not None:
        res["random_test"] = random_test(x, pool, rows, cycles, fwd_all, rng)
    return res


def print_report(title, res):
    b, s = res["baseline"], res["signals"]
    print(f"\n==== {title} ====")
    print(f"랜덤 기준선: 120일 수익률 중앙값 {b['fwd120_median_pct']}%, 평균 {b['fwd120_mean_pct']}%, "
          f"60일 평균 MAE {b['mae60_mean_pct']}%, 진입성공 비율 {b['quality_pass_rate']}")
    print(f"신호 {s['n']}개, 진입성공 비율 {s['quality_pass_rate']}, 라벨 {s['labels']}")
    rt = res.get("random_test")
    if rt:
        print(f"랜덤 비교: 사이클 안 비율 신호 {rt['signal_in_cycle_share']} vs 랜덤 {rt['random_in_cycle_share_mean']} "
              f"(백분위 {rt['in_cycle_percentile']}), 120일 평균 신호 {rt['signal_fwd120_mean_pct']}% vs 랜덤 "
              f"{rt['random_fwd120_mean_pct']}% (백분위 {rt['fwd120_percentile']})")
    for c in res["cycles"]:
        print(f"  {c['cycle']} {c['start']}~{c['end']}: 첫 신호 {c['first_date']}({c['first_lag_td']}, {c['first_label']})"
              f" / 첫 진입성공 신호 {c['first_usable_date']}({c['first_usable_lag_td']})")


# ---------------- 주도 판정 검증 (v2 추가) ----------------
LEAD_WINDOWS = (20, 60)


def _fwd(idx: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(idx), np.nan)
    out[:-k] = idx[k:] / idx[:-k] - 1
    return out


def _excess_stats(ex: np.ndarray, k: int) -> dict:
    """ex: 판정일별 초과수익(소수). 겹치는 구간 때문에 t값은 k일 간격 비중첩 표본으로만 계산"""
    ex = ex[~np.isnan(ex)]
    if len(ex) == 0:
        return {"n": 0}
    sub = ex[::k]
    t = float(np.mean(sub) / (np.std(sub, ddof=1) / np.sqrt(len(sub)))) if len(sub) > 2 and np.std(sub) > 0 else None
    return {"n": int(len(ex)), "mean_excess_pct": round(float(np.mean(ex)) * 100, 2),
            "hit_rate": round(float(np.mean(ex > 0)), 3),
            "n_nonoverlap": int(len(sub)), "t_nonoverlap": None if t is None else round(t, 2)}


def leader_validation(prep, days) -> dict:
    """
    주도 판정이 실제로 앞으로 더 잘 갔는가.
      - 주도 파트: 리더 파트 k일 수익률 - 전체 파트 k일 수익률 평균 (파트 중립 기준)
      - 비교군: 그날 파트 RS 중앙값 1등 파트 (진입조건·히스테리시스 없이 단순 1등)
      - 하닉 본체 주도: 하닉 k일 수익률 - 동일가중 바스켓 k일 수익률
      - 하닉 상대 방어(v2, 추세 OFF + RS 발동선 이상): 같은 방식. 주도와 다르게 움직이는지 확인용
      - 리더 시작일(이벤트) 기준도 따로
    """
    T = len(days)
    parts = prep["part_order"]
    pidx = {pn: subject_index(prep, pn) for pn in parts}
    hy = subject_index(prep, "hynix")
    x = prep["ew_index"]
    res = {}
    for k in LEAD_WINDOWS:
        pf = {pn: _fwd(pidx[pn], k) for pn in parts}
        allp = np.vstack([pf[pn] for pn in parts])
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            part_mean = np.nanmean(allp, axis=0)
        lead_ex, top_ex, start_ex = np.full(T, np.nan), np.full(T, np.nan), []
        prev_leader = None
        for t, d in enumerate(days):
            if d["phase"] == "HOLD":
                prev_leader = None
                continue
            L = d.get("part_leader")
            if L:
                lead_ex[t] = pf[L][t] - part_mean[t]
                if L != prev_leader and not np.isnan(lead_ex[t]):
                    start_ex.append(lead_ex[t])
            prev_leader = L
            ps = d.get("parts") or {}
            if ps:
                top = max(ps, key=lambda pn: ps[pn]["median_rs20"])
                top_ex[t] = pf[top][t] - part_mean[t]
        hf, xf = _fwd(hy, k), _fwd(x, k)
        hl = np.array([bool(d.get("hynix_lead")) for d in days])
        hd = np.array([bool(d.get("hynix_defensive")) for d in days])
        res[f"fwd{k}"] = {
            "part_leader": _excess_stats(lead_ex, k),
            "part_leader_start_events": _excess_stats(np.array(start_ex), 1),
            "naive_top_rs_part": _excess_stats(top_ex, k),
            "hynix_lead": _excess_stats(np.where(hl, hf - xf, np.nan), k),
            "hynix_defensive": _excess_stats(np.where(hd, hf - xf, np.nan), k),
        }
    return res


def state_conditional(prep, days) -> dict:
    """단계별로 그날 이후 바스켓 수익률 평균. 단계가 '설명'을 넘어 앞으로의 수익률을 가르는지 확인"""
    x = prep["ew_index"]
    out = {}
    for k in (60, 120):
        f = _fwd(x, k)
        row = {}
        for ph in ("NORMAL", "EARLY", "CONFIRMED"):
            m = np.array([d["phase"] == ph for d in days]) & ~np.isnan(f)
            row[ph] = {"n": int(m.sum()), "mean_pct": round(float(np.mean(f[m])) * 100, 1) if m.any() else None,
                       "pos_rate": round(float(np.mean(f[m] > 0)), 3) if m.any() else None}
        m = np.array([d["phase"] != "HOLD" for d in days]) & ~np.isnan(f)
        row["ALL"] = {"n": int(m.sum()), "mean_pct": round(float(np.mean(f[m])) * 100, 1),
                      "pos_rate": round(float(np.mean(f[m] > 0)), 3)}
        # 사이클확인 안에서 후반 경고 단계별 (경고가 셀수록 이후 수익률이 낮아야 정상)
        for lv in ("없음", "주의", "후반경고", "강한 후반경고"):
            m = np.array([d["phase"] == "CONFIRMED" and d.get("late_level") == lv for d in days]) & ~np.isnan(f)
            row[f"CONFIRMED·{lv}"] = {"n": int(m.sum()),
                                     "mean_pct": round(float(np.mean(f[m])) * 100, 1) if m.any() else None,
                                     "pos_rate": round(float(np.mean(f[m] > 0)), 3) if m.any() else None}
        out[f"fwd{k}"] = row
    return out


STATE_KEYS = ["phase", "hynix_lead", "hynix_defensive", "part_leader", "active_chains", "late_level"]


def event_validation(prep, days, floor_pct: float) -> dict:
    """
    과거 전 구간에서 실제로 났을 알림 이벤트(describe_change)를 OOS v2 채점 규칙 그대로 채점.
    기준률(base) = 같은 규칙을 '아무 날'에 적용했을 때 통과율. 이벤트 적중률이 기준률보다 높아야 의미 있음.
    z 는 이벤트 독립 가정 → 몰려서 나는 이벤트 때문에 실제보다 과대. 참고만.
    """
    x = prep["ew_index"]
    T = len(days)
    sidx = {pn: subject_index(prep, pn) for pn in prep["part_order"]}
    sidx["hynix"] = subject_index(prep, "hynix")
    groups: dict = {}
    for t in range(1, T):
        a, b = days[t - 1], days[t]
        if "HOLD" in (a["phase"], b["phase"]):
            continue
        _, _, evs = describe_change({k: a.get(k) for k in STATE_KEYS}, {k: b.get(k) for k in STATE_KEYS})
        for e in evs:
            if not e["scoring"]:
                continue
            passed, rec = event_outcome(x, t, e["scoring"], floor_pct, sidx.get(e["subject"]))
            if passed is None:
                continue
            g = groups.setdefault((e["kind"], e["scoring"]), {"pass": [], "metric": []})
            g["pass"].append(passed)
            g["metric"].append(rec.get("basket_fwd60_pct", rec.get("rel_fwd_pct")))

    base = {}
    pool = [t for t in range(T) if days[t]["phase"] != "HOLD"]
    for sc in ("basket_bull", "basket_bear"):
        res = [event_outcome(x, t, sc, floor_pct)[0] for t in pool]
        res = [r for r in res if r is not None]
        base[sc] = float(np.mean(res))
    rel = [event_outcome(x, t, "rel_outperform", floor_pct, sidx[s])[0] for t in pool for s in sidx]
    rel = [r for r in rel if r is not None]
    base["rel_outperform"] = float(np.mean(rel))
    base["rel_underperform"] = 1 - base["rel_outperform"]

    out = {}
    for (kind, sc), g in sorted(groups.items()):
        n, hit, b0 = len(g["pass"]), float(np.mean(g["pass"])), base[sc]
        z = (hit - b0) / np.sqrt(b0 * (1 - b0) / n) if 0 < b0 < 1 else None
        out[f"{kind}|{sc}"] = {"n": n, "hit_rate": round(hit, 3), "base_rate": round(b0, 3),
                               "z_naive": None if z is None else round(float(z), 2),
                               "metric_mean": round(float(np.nanmean(g["metric"])), 2),
                               "metric": "basket_fwd60_pct" if sc.startswith("basket") else "rel_fwd60_pct"}
    out["_base_rates"] = {k: round(v, 3) for k, v in base.items()}
    return out


def print_event_report(ev):
    print("\n==== 알림 이벤트 검증 (OOS v2 채점 규칙 그대로, 기준률 = 아무 날에 같은 규칙 적용) ====")
    print("  basket_bear 적중 = 60거래일 뒤 바스켓 실제 하락. z는 이벤트 독립 가정이라 과대")
    for k, v in ev.items():
        if k.startswith("_"):
            continue
        print(f"  {k}: n={v['n']} 적중 {v['hit_rate']} vs 기준 {v['base_rate']} (z≈{v['z_naive']}), "
              f"{v['metric']} 평균 {v['metric_mean']}")


def print_lead_report(lv, sc):
    print("\n==== 주도 판정 검증 (초과수익 = 이후 k일, 파트는 전체 파트 평균 대비 / 하닉은 바스켓 대비) ====")
    names = {"part_leader": "주도 파트(매일)", "part_leader_start_events": "주도 파트 시작일",
             "naive_top_rs_part": "비교군: RS 1등 파트", "hynix_lead": "하닉 본체 주도",
             "hynix_defensive": "하닉 상대 방어"}
    for k, block in lv.items():
        print(f"  [{k}]")
        for key, st in block.items():
            if not st.get("n"):
                print(f"    {names[key]}: 표본 없음")
                continue
            print(f"    {names[key]}: n={st['n']} 평균 {st['mean_excess_pct']}%p, 적중 {st['hit_rate']}, "
                  f"비중첩 t={st['t_nonoverlap']} (n={st['n_nonoverlap']})")
    print("\n==== 단계별·후반경고별 이후 바스켓 수익률 ====")
    for k, row in sc.items():
        print(f"  [{k}]")
        for ph, v in row.items():
            print(f"    {ph}: n={v['n']} 평균 {v['mean_pct']}% 플러스비율 {v['pos_rate']}")


# ---------------- 그리드 + 하나 빼고 검증 ----------------
GRID = {
    "early_enter_breadth": [0.25, 0.30, 0.35, 0.40, 0.50],
    "new_turn_window": [5, 10],
    "confirm_enter_on_share": [0.50, 0.60, 0.70],
    "confirm_min_parts": [3, 4, 5],
}


def region_of(i, cycles):
    """신호가 어느 사이클 '영역'(직전 사이클 끝 다음날 ~ 이 사이클 끝)에 속하는지"""
    prev_end = -1
    for c in cycles:
        if prev_end < i <= c["end_i"]:
            return c["name"]
        prev_end = c["end_i"]
    return "tail"


def combo_score(res, cycles, include):
    """학습용 점수(사전에 고정): 첫 신호 진입성공 사이클 수 → 탐지 사이클 수 → 헛방 적음 → 진입성공 비율"""
    ct = {c["cycle"]: c for c in res["cycles"]}
    names = [c["name"] for c in cycles if c["name"] in include]
    usable_first = sum(1 for n in names if ct[n]["first_usable_lag_td"] is not None
                       and ct[n]["first_usable_lag_td"] <= DETECT_AFTER_TD)
    detected = sum(1 for n in names if ct[n]["detected"])
    rows = [r for r in res["entries"] if region_of(r["i"], cycles) in include or
            (region_of(r["i"], cycles) == "tail" and "tail" in include)]
    fp = sum(1 for r in rows if r["label"] == "헛방")
    judged = [r["quality_pass"] for r in rows if r["quality_pass"] is not None]
    qr = float(np.mean(judged)) if judged else 0.0
    return (usable_first, detected, -fp, qr)


def run_grid(prep, p, cycles, outdir):
    keys = list(GRID)
    results = []
    for vals in itertools.product(*GRID.values()):
        q = copy.deepcopy(p)
        q.update(dict(zip(keys, vals)))
        res = evaluate(prep, run_engine(prep, q), cycles)
        row = dict(zip(keys, vals))
        row.update({
            "detected": sum(c["detected"] for c in res["cycles"]),
            "first_usable": sum(c["first_usable_date"] is not None for c in res["cycles"]),
            "false_pos": res["signals"]["labels"].get("헛방", 0),
            "late_reentry": res["signals"]["labels"].get("후반 재진입", 0),
            "signals": res["signals"]["n"],
            "quality_rate": res["signals"]["quality_pass_rate"],
        })
        for c in res["cycles"]:
            row[f"{c['cycle']}_lag"] = c["first_lag_td"]
            row[f"{c['cycle']}_label"] = c["first_label"]
        results.append((row, res))
    g = pd.DataFrame([r for r, _ in results])
    g.to_csv(os.path.join(outdir, "grid.csv"), index=False, encoding="utf-8-sig")

    names = [c["name"] for c in cycles]
    loco = []
    for held in names:
        train = [n for n in names if n != held] + ["tail"]
        best_row, best_res = max(results, key=lambda rr: combo_score(rr[1], cycles, train))
        test_ct = next(c for c in best_res["cycles"] if c["cycle"] == held)
        test_rows = [r for r in best_res["entries"] if region_of(r["i"], cycles) == held]
        loco.append({
            "held_out": held,
            **{k: best_row[k] for k in keys},
            "test_detected": test_ct["detected"], "test_first_date": test_ct["first_date"],
            "test_first_label": test_ct["first_label"], "test_first_usable": test_ct["first_usable_date"],
            "test_false_pos": sum(1 for r in test_rows if r["label"] == "헛방"),
        })
    lo = pd.DataFrame(loco)
    lo.to_csv(os.path.join(outdir, "loco.csv"), index=False, encoding="utf-8-sig")
    print("\n==== 하나 빼고 검증 (빠진 사이클로 테스트) ====")
    print(lo.to_string(index=False))
    stable = lo[keys].nunique().to_dict()
    print(f"선택된 파라미터가 사이클마다 몇 가지로 갈렸나: {stable} (1이면 안정적)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="naver")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--suggest-list-dates", action="store_true",
                    help="첫 일봉이 늦은 종목을 list_date 후보로 출력 (KRX 상장일과 대조 후 universe.json 에 직접 기입)")
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
        except Exception as e:
            print(f"  {s['name']}: 수신 실패 {e}")
    kospi = prov.get_index_ohlcv(universe["benchmark"]["index"], start, end)

    ok, bad, report, suggest = check_coverage(prices, universe, start, require_all=True)
    print(f"\n요청 시작일 {start}")
    for t, r in report.items():
        print(f"  {r['name']}: 기대 {r['expected_from']} / 실제 첫 일봉 {r['first']}")
    if args.suggest_list_dates:
        print("\nlist_date 후보 (반드시 KRX 상장일과 대조. 다르면 데이터 잘림이니 기입 금지):")
        print(json.dumps(suggest, ensure_ascii=False, indent=2))
        return
    if not ok:
        raise SystemExit("[중단] 커버리지 문제 → 결과 신뢰 불가:\n  " + "\n  ".join(bad) +
                         "\n진짜 늦은 상장이면 universe.json 에 list_date 기입. --suggest-list-dates 로 후보 확인.")

    outdir = os.path.join(HERE, "backtest")
    os.makedirs(outdir, exist_ok=True)
    prep = prepare(universe, prices, kospi, p, start=start)
    days = run_engine(prep, p)
    pd.DataFrame([{k: d.get(k) for k in ["date", "phase", "hynix_lead", "hynix_defensive", "part_leader", "late_level",
                                         "breadth_early", "parts_active", "on_share", "vol_share"]}
                  for d in days]).to_csv(os.path.join(outdir, "daily_states.csv"), index=False, encoding="utf-8-sig")

    rng = np.random.default_rng(42)
    ac = auto_cycles(prep["ew_index"], prep["dates"])
    print("\n자동 정의 사이클 (동일가중 지수 기준):")
    for c in ac:
        print(f"  {c['name']}: {c['start']} → {c['end']}  +{c['gain_pct']}%{' (진행중)' if c['ongoing'] else ''}")

    res_auto = evaluate(prep, days, ac, rng)
    res_man = evaluate(prep, days, manual_cycles(prep["dates"]), rng)
    print_report("자동 사이클 기준 (메인)", res_auto)
    print_report("수동 사이클 기준 (비교용)", res_man)

    pd.DataFrame(res_auto["entries"]).drop(columns=["i"]).to_csv(
        os.path.join(outdir, "signals_auto.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(res_man["entries"]).drop(columns=["i"]).to_csv(
        os.path.join(outdir, "signals_manual.csv"), index=False, encoding="utf-8-sig")
    lv = leader_validation(prep, days)
    sc = state_conditional(prep, days)
    print_lead_report(lv, sc)
    pol = load(os.path.join(HERE, "oos_evaluation_policy_v2.json"))
    ev = event_validation(prep, days, float(pol["scoring_rules"]["fwd120_floor_pct"]))
    print_event_report(ev)
    summary = {"quality_rule": QUALITY, "auto_cycle_rule": AUTO_CYCLE, "auto_cycles": ac,
               "leader_validation": lv, "state_conditional": sc, "event_validation": ev,
               "auto": {k: v for k, v in res_auto.items() if k != "entries"},
               "manual": {k: v for k, v in res_man.items() if k != "entries"}}
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    if args.grid:
        run_grid(prep, p, ac, outdir)


if __name__ == "__main__":
    main()
