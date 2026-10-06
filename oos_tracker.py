"""표본 밖(OOS) 알림 기록·성숙 채점기.

매일 run_daily.py 뒤에 실행한다.
- output/history.csv 를 고정 스키마로 유지
- latest.json 의 실제 알림 종류를 history.csv 에 명시
- 동결일 이후 성과 채점 대상 알림이 120거래일 성숙하면
  동일가중 바스켓 기준 MAE60 / 120일 수익률 / 고정 pass 여부를 자동 기록

판정 기준은 oos_evaluation_policy.json 이 유일한 authority다.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from data_provider import check_coverage, get_provider
from regime import prepare

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = [
    "asof", "phase", "leader", "active_chains", "late_level",
    "breadth_early", "on_share", "data_ok",
    "alert_changed", "alert_kinds", "oos_eligible",
    "score_status", "mae60_pct", "fwd120_pct", "quality_pass", "maturity_asof",
    "alert",
]


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def b(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def alert_kinds(message: str) -> list[str]:
    m = message or ""
    kinds = []
    if "[데이터 오류]" in m:
        kinds.append("data_error")
    if "[데이터 정상화]" in m:
        kinds.append("data_recovery")
    if "단계:" in m:
        kinds.append("phase_change")
    if "SK하이닉스 본체 주도" in m or "주도 파트:" in m:
        kinds.append("leader_change")
    if "후반 경고:" in m:
        kinds.append("late_change")
    if "체인 활성화" in m or "체인 비활성화" in m:
        kinds.append("chain_change")
    return kinds


def read_history(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_history(path: str, rows: list[dict]):
    with open(path, "w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: r.get(k, "") for k in FIELDS})


def normalize(rows: list[dict], policy: dict):
    start = policy["oos_start_date"]
    scored_kinds = set(policy["alert_logging"]["performance_scored_kinds"])
    for r in rows:
        msg = r.get("alert", "")
        kinds = [x for x in r.get("alert_kinds", "").split("|") if x] or alert_kinds(msg)
        changed = b(r.get("alert_changed")) or bool(msg and "기준선 생성" not in msg)
        r["alert_changed"] = str(bool(changed))
        r["alert_kinds"] = "|".join(kinds)
        eligible = bool(r.get("asof") and r["asof"] >= start and changed and scored_kinds.intersection(kinds))
        r["oos_eligible"] = str(eligible)
        if not eligible and not r.get("score_status"):
            r["score_status"] = "not_eligible"


def merge_latest(rows: list[dict], latest: dict, policy: dict):
    asof = latest.get("asof")
    if not asof:
        return
    state = latest.get("state") or {}
    market = latest.get("market") or {}
    alert = latest.get("alert") or {}
    changed = bool(alert.get("changed"))
    msg = alert.get("message") or ""
    kinds = alert_kinds(msg)
    scored_kinds = set(policy["alert_logging"]["performance_scored_kinds"])
    eligible = bool(asof >= policy["oos_start_date"] and changed and scored_kinds.intersection(kinds))

    existing = next((r for r in reversed(rows) if r.get("asof") == asof), None)
    if existing is None:
        existing = {"asof": asof}
        rows.append(existing)

    # 같은 날 재실행(is_new_bar=false)이 실제 장마감 알림을 지우지 않게 한다.
    preserve_event = b(existing.get("alert_changed")) and not latest.get("is_new_bar", False)
    existing.update({
        "phase": state.get("phase", existing.get("phase", "")),
        "leader": state.get("leader", existing.get("leader", "")),
        "active_chains": "|".join(state.get("active_chains") or []),
        "late_level": state.get("late_level", existing.get("late_level", "")),
        "breadth_early": market.get("breadth_early", existing.get("breadth_early", "")),
        "on_share": market.get("on_share", existing.get("on_share", "")),
        "data_ok": str(bool(latest.get("data_ok"))),
    })
    if not preserve_event:
        existing["alert_changed"] = str(changed)
        existing["alert_kinds"] = "|".join(kinds)
        existing["oos_eligible"] = str(eligible)
        existing["alert"] = msg
        if eligible and existing.get("score_status") in ("", "not_eligible", None):
            existing["score_status"] = "pending_120td"
        elif not eligible and not existing.get("score_status"):
            existing["score_status"] = "not_eligible"


def score_matured(rows: list[dict], policy: dict, provider_name: str):
    pending = [r for r in rows if b(r.get("oos_eligible")) and r.get("score_status") != "scored"]
    if not pending:
        print("OOS: 성숙 대기 신호 없음")
        return

    earliest = min(dt.date.fromisoformat(r["asof"]) for r in pending)
    # 지표/유효성 워밍업을 충분히 확보한다.
    start = earliest - dt.timedelta(days=450)
    end = dt.date.today()
    universe = load_json(os.path.join(HERE, "universe.json"))
    params = load_json(os.path.join(HERE, "params.json"))
    provider = get_provider(provider_name)

    prices = {}
    for s in universe["stocks"]:
        prices[s["ticker"]] = provider.get_stock_ohlcv(s["ticker"], start, end)
    kospi = provider.get_index_ohlcv(universe["benchmark"]["index"], start, end)
    ok, bad, _, _ = check_coverage(prices, universe, start, require_all=True)
    if not ok:
        print("OOS 채점 보류: coverage 문제: " + " / ".join(bad))
        return

    prep = prepare(universe, prices, kospi, params, start=start)
    dates = prep["dates"]
    x = prep["ew_index"]
    q = policy["quality_rule"]
    mae_w = int(q["mae_window_trading_days"])
    fwd_w = int(q["forward_window_trading_days"])
    mae_floor = float(q["mae_floor_pct"])
    fwd_floor = float(q["fwd120_floor_pct"])

    date_to_i = {d.date().isoformat(): i for i, d in enumerate(dates)}
    for r in pending:
        i = date_to_i.get(r["asof"])
        if i is None:
            r["score_status"] = "date_not_found"
            continue
        if i + fwd_w >= len(x):
            r["score_status"] = "pending_120td"
            continue
        seg60 = x[i + 1:i + mae_w + 1] / x[i] - 1
        fwd120 = x[i + fwd_w] / x[i] - 1
        mae60 = float(np.min(seg60))
        passed = bool(mae60 * 100 >= mae_floor and fwd120 * 100 >= fwd_floor)
        r.update({
            "score_status": "scored",
            "mae60_pct": f"{mae60 * 100:.2f}",
            "fwd120_pct": f"{fwd120 * 100:.2f}",
            "quality_pass": str(passed),
            "maturity_asof": dates[i + fwd_w].date().isoformat(),
        })
        print(f"OOS 채점 {r['asof']} {r.get('alert_kinds')}: MAE60 {mae60*100:.2f}% / fwd120 {fwd120*100:.2f}% / pass={passed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="naver")
    ap.add_argument("--outdir", default=os.path.join(HERE, "output"))
    args = ap.parse_args()

    policy = load_json(os.path.join(HERE, "oos_evaluation_policy.json"))
    if not policy or policy.get("status") != "FROZEN_OOS_POLICY_V1":
        raise SystemExit("OOS policy missing or not frozen")

    hist_path = os.path.join(args.outdir, "history.csv")
    latest = load_json(os.path.join(args.outdir, "latest.json"), {}) or {}
    rows = read_history(hist_path)
    normalize(rows, policy)
    merge_latest(rows, latest, policy)
    score_matured(rows, policy, args.provider)
    rows.sort(key=lambda r: r.get("asof", ""))
    write_history(hist_path, rows)


if __name__ == "__main__":
    main()
