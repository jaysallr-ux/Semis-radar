"""TOP detector validation scaffold.

Required evaluation discipline:
- No auto-cycle trough is an input to the detector; labels are scoring-only.
- A2 (COVID shock) is reported separately and excluded from endogenous fit score.
- A4/A5 are marked contaminated because they motivated this redesign.
- Report 60/120d max drawdown AND max upside after alert.
- Compare against random eligible dates.
- Leave-one-cycle-out is mandatory; never select thresholds on aggregate A1-A5 alone.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pandas as pd

from top import run_top_detector


def forward_excursions(index: pd.Series, i: int, windows=(60, 120)):
    base = float(index.iloc[i])
    out = {}
    for w in windows:
        f = index.iloc[i+1:i+1+w] / base - 1
        out[str(w)] = {
            "max_up": None if f.empty else float(f.max()),
            "max_drawdown": None if f.empty else float(f.min()),
        }
    return out


def score_alerts(rows, top_rows, ew_index, cycle_labels, p):
    results = []
    for c in cycle_labels:
        peak = pd.Timestamp(c["peak_date"])
        kind = "external_shock" if c["id"] in p["external_shock_cycles"] else "endogenous"
        contaminated = c["id"] in p["contaminated_cycles"]
        candidates = []
        for i, (r, t) in enumerate(zip(rows, top_rows)):
            if t["top_state"] != "CONFIRMED":
                continue
            d = pd.Timestamp(r["date"])
            delta = (d-peak).days
            if p["target_window_start"] <= delta <= p["target_window_end"]:
                candidates.append((i, d, delta))
        hit = min(candidates, key=lambda x: abs(x[2])) if candidates else None
        rec = {"cycle": c["id"], "kind": kind, "contaminated": contaminated,
               "peak_date": str(peak.date()), "hit": hit is not None}
        if hit:
            i, d, delta = hit
            rec.update(alert_date=str(d.date()), calendar_days_from_peak=delta,
                       excursions=forward_excursions(ew_index, i, p["forward_windows"]))
        results.append(rec)
    return results


def random_baseline(rows, ew_index, eligible, p, seed=20261007):
    rng = random.Random(seed)
    ids = [i for i, ok in enumerate(eligible) if ok and i < len(rows)-max(p["forward_windows"])]
    if not ids:
        return []
    return [forward_excursions(ew_index, rng.choice(ids), p["forward_windows"])
            for _ in range(p["random_trials"])]


def loco_plan(cycles, p):
    """Explicit folds. Threshold selection must use train only; held-out score is final."""
    endogenous = [c for c in cycles if c["id"] not in p["external_shock_cycles"]]
    return [{"held_out": c["id"],
             "train": [x["id"] for x in endogenous if x["id"] != c["id"]],
             "held_out_contaminated": c["id"] in p["contaminated_cycles"]}
            for c in endogenous]


def main():
    p = json.loads(Path("params_top.json").read_text(encoding="utf-8"))
    # This file deliberately does not guess labels or reconstruct missing market
    # series from daily_states.csv. Integrate with the existing backtest data
    # builder so rows include ew_index and optional new_high_share causally.
    raise SystemExit("Research scaffold installed. Wire to existing backtest data builder before scoring; do not use production alerts yet.")


if __name__ == "__main__":
    main()
