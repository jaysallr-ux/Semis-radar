"""Causal TOP detector research module.

No future cycle trough/peak labels are consumed here.  The detector only sees the
stream of records available through the current date.  It is intentionally
separate from regime.py until out-of-sample validation is satisfactory.
"""
from __future__ import annotations

from collections import deque


def _max_valid(xs):
    vals = [x for x in xs if x is not None]
    return max(vals) if vals else None


def _leader_rs(r):
    if r.get("hynix_lead"):
        return (r.get("hynix") or {}).get("rs20_vs_peers")
    leader = r.get("part_leader")
    return ((r.get("parts") or {}).get(leader) or {}).get("median_rs20") if leader else None


def _new_high_share(r):
    """Optional causal input. Returns None unless caller supplies new_high_share."""
    return r.get("new_high_share")


def run_top_detector(rows: list[dict], p: dict) -> list[dict]:
    """Return TOP state for each row without mutating regime output.

    State sequence: OFF -> WATCH -> CONFIRMED. WATCH expires if confirmation does
    not arrive within watch_memory_days. Maturity uses only observable history:
    days since CONFIRMED and ew_index gain since the current CONFIRMED episode.
    """
    w = int(p["memory_window"])
    hist = deque(maxlen=w)
    confirmed_start = None
    confirmed_ew0 = None
    state = "OFF"
    watch_age = 0
    out = []

    for i, r in enumerate(rows):
        phase = r.get("phase")
        ew = r.get("ew_index")
        if phase == "CONFIRMED":
            if confirmed_start is None:
                confirmed_start, confirmed_ew0 = i, ew
        else:
            confirmed_start = confirmed_ew0 = None
            state, watch_age = "OFF", 0

        hist.append({
            "on": r.get("on_share"),
            "leader_rs": _leader_rs(r),
            "nh": _new_high_share(r),
        })
        peak_on = _max_valid([x["on"] for x in hist])
        peak_rs = _max_valid([x["leader_rs"] for x in hist])
        peak_nh = _max_valid([x["nh"] for x in hist])
        cur_on, cur_rs, cur_nh = r.get("on_share"), _leader_rs(r), _new_high_share(r)

        conf_days = 0 if confirmed_start is None else i - confirmed_start + 1
        ew_gain = None
        if ew is not None and confirmed_ew0 not in (None, 0):
            ew_gain = ew / confirmed_ew0 - 1
        mature = phase == "CONFIRMED" and (
            conf_days >= p["maturity_min_confirmed_days"] or
            (ew_gain is not None and ew_gain >= p["maturity_min_ew_gain"])
        )

        watch_items = {
            "breadth_peak_then_fade": bool(peak_on is not None and peak_on >= p["watch_on_share_peak_min"] and cur_on is not None and peak_on-cur_on >= p["watch_on_share_drop"]),
            "leader_rs_peak_then_fade": bool(peak_rs is not None and cur_rs is not None and peak_rs-cur_rs >= p["watch_leader_rs_drop"]),
            "new_highs_peak_then_fade": bool(peak_nh is not None and cur_nh is not None and peak_nh-cur_nh >= p["watch_new_high_share_drop"]),
        }
        if mature and sum(watch_items.values()) >= p["watch_min_items"] and state == "OFF":
            state, watch_age = "WATCH", 0

        if state == "WATCH":
            watch_age += 1
            leader_break = not bool((r.get("hynix") or {}).get("trend")) if r.get("hynix_lead") else r.get("part_leader") is None
            confirm_items = {
                "breadth_break": bool(peak_on is not None and cur_on is not None and peak_on-cur_on >= p["confirm_on_share_drop"]),
                "leader_trend_break": leader_break,
                "part_weakening": r.get("part_leader") is None,
            }
            if sum(confirm_items.values()) >= p["confirm_min_items"]:
                state = "CONFIRMED"
            elif watch_age > p["watch_memory_days"]:
                state, watch_age = "OFF", 0
        else:
            confirm_items = {}

        out.append({
            "date": r.get("date"), "top_state": state, "mature": mature,
            "confirmed_days": conf_days, "ew_gain_since_confirmed": ew_gain,
            "watch_items": watch_items, "confirm_items": confirm_items,
            "diagnostics": {"peak_on_share": peak_on, "peak_leader_rs": peak_rs, "peak_new_high_share": peak_nh},
        })
    return out
