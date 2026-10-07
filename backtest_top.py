"""Causal TOP detector backtest.

PRE-REGISTERED discipline:
- detector never consumes future cycle labels;
- A2 external shock is reported separately;
- A4/A5 contamination is disclosed, never hidden;
- only three detector parameters may vary in LOCO;
- pass/reject thresholds come only from params_top.json;
- correction warnings are future-labelled for scoring only and are neutral.
"""
from __future__ import annotations

import copy
import datetime as dt
import itertools
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import auto_cycles
from data_provider import check_coverage, get_provider
from regime import prepare, run_engine
from top import run_top_detector

HERE = Path(__file__).resolve().parent
OUT = HERE / "backtest_top"

# Frozen before first TOP result is observed. Exactly three tunable parameters.
TUNING_GRID = {
    "watch_on_share_drop": [0.15, 0.20, 0.25],
    "watch_leader_rs_drop": [4.0, 6.0, 8.0],
    "confirm_on_share_drop": [0.25, 0.30, 0.35],
}


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def build_rows(prep, days):
    """Attach only same-day causal inputs needed by TOP."""
    rows = []
    semi = prep["is_semi"]
    for i, d in enumerate(days):
        r = copy.deepcopy(d)
        r["ew_index"] = float(prep["ew_index"][i])
        v = prep["valid"][i] & semi
        dist = prep["arr"]["dist52"][i]
        ok = v & ~np.isnan(dist)
        # dist52 uses a trailing rolling high including today, so ==0 is a causal 52w close high.
        r["new_high_share"] = float(np.mean(dist[ok] >= -1e-12)) if ok.any() else None
        rows.append(r)
    return rows


def cycle_labels(prep):
    ac = auto_cycles(prep["ew_index"], prep["dates"])
    out = []
    for c in ac:
        out.append({
            "id": c["name"], "start_i": c["start_i"], "peak_i": c["end_i"],
            "start_date": c["start"], "peak_date": c["end"], "ongoing": c["ongoing"],
            "gain_pct": c["gain_pct"],
        })
    return out


def confirmed_events(top_rows):
    """Count transitions into TOP CONFIRMED, not every day it remains confirmed."""
    return [i for i, t in enumerate(top_rows)
            if t["top_state"] == "CONFIRMED" and (i == 0 or top_rows[i-1]["top_state"] != "CONFIRMED")]


def excursion(index, i, w):
    if i + 1 >= len(index):
        return {"max_up": None, "max_drawdown": None}
    f = index[i+1:min(len(index), i+1+w)] / index[i] - 1
    return {"max_up": None if len(f) == 0 else float(np.max(f)),
            "max_drawdown": None if len(f) == 0 else float(np.min(f))}


def correction_windows(index, cycles, p):
    """Ex-post scoring labels only: >=15% drawdown from running high, later full recovery/new high."""
    dd_min = float(p["correction_warning"]["drawdown_from_prior_high_min"])
    windows = []
    for c in cycles:
        s, e = c["start_i"], c["peak_i"]
        if e <= s:
            continue
        running_hi_i = s
        j = s + 1
        while j < e:
            if index[j] >= index[running_hi_i]:
                running_hi_i = j
                j += 1
                continue
            if index[j] <= index[running_hi_i] * (1-dd_min):
                draw_start = j
                recovery = next((k for k in range(j+1, e+1) if index[k] > index[running_hi_i]), None)
                if recovery is not None:
                    windows.append({"cycle": c["id"], "start_i": draw_start,
                                    "end_i": recovery, "prior_high_i": running_hi_i})
                    running_hi_i = recovery
                    j = recovery + 1
                    continue
            j += 1
    return windows


def in_correction(i, windows):
    return next((w for w in windows if w["start_i"] <= i <= w["end_i"]), None)


def hit_cycle(i, cycles, p):
    a, b = int(p["target_window_start"]), int(p["target_window_end"])
    d = pd.Timestamp
    date_i = None
    # caller supplies calendar mapping separately; this helper unused intentionally.
    return None


def evaluate(rows, top_rows, prep, cycles, p, seed=20261007):
    dates = prep["dates"]
    index = prep["ew_index"]
    events = confirmed_events(top_rows)
    corr = correction_windows(index, cycles, p)
    external = set(p["external_shock_cycles"])
    endogenous = [c for c in cycles if c["id"] not in external]

    # Hit window uses calendar days exactly as pre-registered (-60,+20).
    cycle_hits = []
    used_events = set()
    for c in cycles:
        peak = pd.Timestamp(c["peak_date"])
        candidates = []
        for i in events:
            delta = (pd.Timestamp(dates[i]) - peak).days
            if p["target_window_start"] <= delta <= p["target_window_end"]:
                candidates.append((abs(delta), i, delta))
        best = min(candidates) if candidates else None
        rec = {"cycle": c["id"], "kind": "external_shock" if c["id"] in external else "endogenous",
               "contaminated": c["id"] in p["contaminated_cycles"], "peak_date": c["peak_date"],
               "hit": best is not None}
        if best:
            _, i, delta = best
            used_events.add(i)
            rec.update(alert_date=str(pd.Timestamp(dates[i]).date()), calendar_days_from_peak=int(delta),
                       excursions={str(w): excursion(index, i, int(w)) for w in p["forward_windows"]})
        cycle_hits.append(rec)

    # Classify non-hit CONFIRMED events. Correction warnings are neutral.
    event_rows = []
    false_events = []
    correction_events = []
    for i in events:
        if i in used_events:
            cls = "TOP_HIT"
        else:
            cw = in_correction(i, corr)
            if cw is not None:
                cls = "CORRECTION_WARNING"
                correction_events.append(i)
            else:
                cls = "OTHER_CONFIRMED"
                false_events.append(i)
        ex20 = excursion(index, i, int(p["false_breakout_days"]))
        ex60 = excursion(index, i, 60)
        event_rows.append({"i": i, "date": str(pd.Timestamp(dates[i]).date()), "class": cls,
                           "max_up20": ex20["max_up"], "max_up60": ex60["max_up"],
                           "max_drawdown60": ex60["max_drawdown"]})

    # Strict false-confirm definition: non-hit/non-correction TOP followed by +10% within 20 trading days.
    false_breakouts = [r for r in event_rows if r["class"] == "OTHER_CONFIRMED"
                       and r["max_up20"] is not None and r["max_up20"] >= p["false_breakout_gain"]]
    judged_non_neutral = [r for r in event_rows if r["class"] != "CORRECTION_WARNING"]
    false_rate = len(false_breakouts) / len(judged_non_neutral) if judged_non_neutral else 0.0

    # Value test: use hit alerts from endogenous cycles only and compare with eligible random dates.
    hit_indices = []
    for h in cycle_hits:
        if h["kind"] == "endogenous" and h["hit"]:
            hit_indices.append(next(i for i in events if str(pd.Timestamp(dates[i]).date()) == h["alert_date"]))
    actual_dd = [excursion(index, i, 60)["max_drawdown"] for i in hit_indices]
    actual_up = [excursion(index, i, 60)["max_up"] for i in hit_indices]
    actual_dd = [x for x in actual_dd if x is not None]
    actual_up = [x for x in actual_up if x is not None]

    eligible = [i for i, r in enumerate(rows)
                if r.get("phase") == "CONFIRMED" and i + 60 < len(rows)]
    rng = np.random.default_rng(seed)
    draws = int(p["random_trials"])
    rand_dd, rand_up = [], []
    if eligible:
        picks = rng.choice(eligible, size=draws, replace=True)
        for i in picks:
            e = excursion(index, int(i), 60)
            rand_dd.append(e["max_drawdown"])
            rand_up.append(e["max_up"])
    # Deeper drawdown means more negative. Percentile is share of random dates with a shallower drawdown.
    mean_dd = float(np.mean(actual_dd)) if actual_dd else None
    mean_up = float(np.mean(actual_up)) if actual_up else None
    dd_pct = (100.0 * np.mean(np.array(rand_dd) > mean_dd)) if rand_dd and mean_dd is not None else None
    random_up_mean = float(np.mean(rand_up)) if rand_up else None

    return {"cycles": cycle_hits, "events": event_rows,
            "counts": {"confirmed_events": len(events), "top_hits": len(used_events),
                       "correction_warnings": len(correction_events), "false_breakouts": len(false_breakouts),
                       "judged_non_neutral": len(judged_non_neutral)},
            "false_confirm_rate": false_rate,
            "value": {"endogenous_hit_n": len(hit_indices), "mean_max_drawdown60": mean_dd,
                      "drawdown60_random_percentile": dd_pct, "mean_max_up60": mean_up,
                      "random_mean_max_up60": random_up_mean}}


def train_score(ev, train_ids):
    hits = sum(1 for c in ev["cycles"] if c["cycle"] in train_ids and c["hit"])
    # Tie-break only; does not add tunable dimensions.
    return (hits, -ev["false_confirm_rate"])


def run_loco(rows, prep, cycles, p):
    endogenous = [c for c in cycles if c["id"] not in p["external_shock_cycles"]]
    keys = list(TUNING_GRID)
    assert keys == p["tunable_parameters"] and len(keys) <= p["max_tunable_parameters"]
    combos = []
    for vals in itertools.product(*(TUNING_GRID[k] for k in keys)):
        q = copy.deepcopy(p)
        q.update(dict(zip(keys, vals)))
        tr = run_top_detector(rows, q)
        ev = evaluate(rows, tr, prep, cycles, q)
        combos.append((dict(zip(keys, vals)), ev))

    folds = []
    for held in endogenous:
        train_ids = [c["id"] for c in endogenous if c["id"] != held["id"]]
        params, ev = max(combos, key=lambda z: train_score(z[1], train_ids))
        hc = next(c for c in ev["cycles"] if c["cycle"] == held["id"])
        folds.append({"held_out": held["id"], "contaminated": held["id"] in p["contaminated_cycles"],
                      "selected": params, "hit": hc["hit"], "alert_date": hc.get("alert_date"),
                      "calendar_days_from_peak": hc.get("calendar_days_from_peak")})
    return folds


def verdict(base_eval, folds, p):
    a = p["acceptance"]
    endo = [f for f in folds if f["held_out"] in ("A1", "A3", "A4", "A5")]
    loco_hits = sum(bool(f["hit"]) for f in endo)
    v = base_eval["value"]
    tests = {
        "detection": len(endo) == a["loco_endogenous_total"] and loco_hits >= a["loco_endogenous_hits_min"],
        "value": (v["drawdown60_random_percentile"] is not None and
                  v["drawdown60_random_percentile"] >= a["drawdown60_random_percentile_min"] and
                  v["mean_max_up60"] is not None and v["random_mean_max_up60"] is not None and
                  v["mean_max_up60"] < v["random_mean_max_up60"]),
        "false_alerts": base_eval["false_confirm_rate"] <= a["false_confirm_rate_max"],
        "degrees_of_freedom": len(TUNING_GRID) <= p["max_tunable_parameters"],
    }
    return {"tests": tests, "loco_hits": loco_hits, "loco_total": len(endo),
            "decision": "OPERATING_CANDIDATE" if all(tests.values()) else a["failure_action"]}


def main():
    p = load("params_top.json")
    prod = load("params.json")
    universe = load("universe.json")
    start = dt.date(2015, 1, 1)
    end = dt.date.today()
    prov = get_provider("naver")
    prices = {}
    for s in universe["stocks"]:
        prices[s["ticker"]] = prov.get_stock_ohlcv(s["ticker"], start, end)
    kospi = prov.get_index_ohlcv(universe["benchmark"]["index"], start, end)
    ok, bad, _, _ = check_coverage(prices, universe, start, require_all=True)
    if not ok:
        raise SystemExit("coverage failure: " + "; ".join(bad))
    prep = prepare(universe, prices, kospi, prod, start=start)
    days = run_engine(prep, prod)
    rows = build_rows(prep, days)
    cycles = cycle_labels(prep)
    top_rows = run_top_detector(rows, p)
    base_eval = evaluate(rows, top_rows, prep, cycles, p)
    folds = run_loco(rows, prep, cycles, p)
    vd = verdict(base_eval, folds, p)
    result = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "policy_frozen": True, "tuning_grid": TUNING_GRID,
              "cycles": cycles, "base": base_eval, "loco": folds, "verdict": vd}
    OUT.mkdir(exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(base_eval["events"]).drop(columns=["i"], errors="ignore").to_csv(OUT / "events.csv", index=False, encoding="utf-8-sig")
    print(json.dumps({"verdict": vd, "value": base_eval["value"], "counts": base_eval["counts"],
                      "false_confirm_rate": base_eval["false_confirm_rate"], "loco": folds},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
