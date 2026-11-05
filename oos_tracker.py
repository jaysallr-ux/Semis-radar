"""표본 밖(OOS) 기록·성숙 채점기 v2.

매일 run_daily.py 뒤에 실행한다.
- output/history.csv : 일별 상태 기록. 이 스크립트가 단독으로 쓴다 (run_daily 는 안 씀)
- output/oos_events.csv : latest.json alert.events 를 이벤트 단위로 기록하고, 성숙하면 방향별 규칙으로 채점

판정 기준은 oos_evaluation_policy_v2.json 이 유일한 authority다.
V1 정책(oos_evaluation_policy.json)은 하락 방향 알림까지 매수 품질로 채점하던 결함 때문에 폐기.
V1 대기 행은 history.csv 에 v1_retired 로 남는다.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os

import numpy as np

from data_provider import check_coverage, get_provider
from regime import prepare, subject_index

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_FILE = "oos_evaluation_policy_v2.json"
HIST_FIELDS = [
    "asof", "phase", "leader", "active_chains", "late_level",
    "breadth_early", "on_share", "data_ok",
    "alert_changed", "alert_kinds", "oos_eligible",
    "score_status", "mae60_pct", "fwd120_pct", "quality_pass", "maturity_asof",
    "alert",
]
EV_FIELDS = [
    "asof", "engine", "kind", "scoring", "subject", "msg", "status",
    "basket_mae60_pct", "basket_fwd60_pct", "basket_fwd120_pct", "subject_fwd_pct", "basket_fwd_pct", "rel_fwd_pct",
    "pass", "maturity_asof",
]


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def b(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path: str, rows: list[dict], fields: list[str]):
    with open(path, "w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: r.get(k, "") for k in fields})


# ---------------- history.csv (일별 기록) ----------------
def retire_v1(rows: list[dict], policy: dict):
    start = policy["oos_start_date"]
    for r in rows:
        if r.get("asof", "") < start and r.get("score_status") == "pending_120td":
            r["score_status"] = "v1_retired"


def merge_history(rows: list[dict], latest: dict):
    asof = latest.get("asof")
    if not asof:
        return
    state = latest.get("state") or {}
    market = latest.get("market") or {}
    alert = latest.get("alert") or {}
    events = alert.get("events") or []
    existing = next((r for r in reversed(rows) if r.get("asof") == asof), None)
    if existing is None:
        existing = {"asof": asof}
        rows.append(existing)
    existing.update({
        "phase": state.get("phase", ""),
        "leader": state.get("leader", ""),
        "active_chains": "|".join(state.get("active_chains") or []),
        "late_level": state.get("late_level", ""),
        "breadth_early": market.get("breadth_early", ""),
        "on_share": market.get("on_share", ""),
        "data_ok": str(bool(latest.get("data_ok"))),
    })
    # 같은 날 재실행(is_new_bar=False)이 그날 알림 기록을 지우지 않게
    if latest.get("is_new_bar", False) or not b(existing.get("alert_changed")):
        existing["alert_changed"] = str(bool(alert.get("changed")))
        existing["alert_kinds"] = "|".join(dict.fromkeys(e["kind"] for e in events))
        existing["oos_eligible"] = str(any(e.get("scoring") for e in events))
        existing["score_status"] = "v2_see_oos_events" if any(e.get("scoring") for e in events) else "not_eligible"
        existing["alert"] = alert.get("message", "")


# ---------------- oos_events.csv (이벤트 채점) ----------------
def merge_events(ev_rows: list[dict], latest: dict, policy: dict):
    asof = latest.get("asof")
    alert = latest.get("alert") or {}
    if not asof or asof < policy["oos_start_date"] or latest.get("engine") != policy["engine"]:
        return
    if not alert.get("changed"):
        return
    seen = {(r["asof"], r["kind"], r.get("subject", ""), r["msg"]) for r in ev_rows}
    for e in alert.get("events") or []:
        key = (asof, e["kind"], e.get("subject") or "", e["msg"])
        if key in seen:
            continue
        ev_rows.append({
            "asof": asof, "engine": latest.get("engine"), "kind": e["kind"],
            "scoring": e.get("scoring") or "", "subject": e.get("subject") or "", "msg": e["msg"],
            "status": "pending" if e.get("scoring") else "not_scored",
        })
        seen.add(key)


MATURITY = {"basket_bull": 120, "basket_bear": 60, "rel_outperform": 60, "rel_underperform": 60}


def event_outcome(x: np.ndarray, i: int, scoring: str, floor_pct: float, si: np.ndarray | None = None):
    """
    이벤트 하나 채점. OOS 채점기와 백테스트가 같은 함수를 쓴다.
    반환: (passed, 기록용 dict) / 성숙 전·데이터 없음이면 (None, 사유 dict)
      basket_bull     : MAE60 >= -10% AND fwd120 >= floor
      basket_bear     : fwd60 <= 0  (60거래일 뒤 바스켓이 실제로 내려가 있어야 경고 적중)
                        ※ v2 초안의 '매수 품질 불통과'는 +10% 올라도 적중으로 세서 폐기
      rel_outperform  : 주체 fwd60 - 바스켓 fwd60 > 0
      rel_underperform: 주체 fwd60 - 바스켓 fwd60 < 0
    """
    w = MATURITY[scoring]
    if i + w >= len(x):
        return None, {"status": "pending"}
    rec = {}
    if scoring in ("basket_bull", "basket_bear"):
        mae60 = float(np.min(x[i + 1:i + 61] / x[i] - 1)) * 100
        fwd60 = float(x[i + 60] / x[i] - 1) * 100
        rec.update(basket_mae60_pct=round(mae60, 2), basket_fwd60_pct=round(fwd60, 2))
        if i + 120 < len(x):
            rec["basket_fwd120_pct"] = round(float(x[i + 120] / x[i] - 1) * 100, 2)
        if scoring == "basket_bull":
            passed = mae60 >= -10.0 and rec["basket_fwd120_pct"] >= floor_pct
        else:
            passed = fwd60 <= 0
    else:
        if si is None or np.isnan(si[i]) or np.isnan(si[i + w]):
            return None, {"status": "subject_data_missing"}
        sf = float(si[i + w] / si[i] - 1) * 100
        bf = float(x[i + w] / x[i] - 1) * 100
        rel = sf - bf
        passed = rel > 0 if scoring == "rel_outperform" else rel < 0
        rec.update(subject_fwd_pct=round(sf, 2), basket_fwd_pct=round(bf, 2), rel_fwd_pct=round(rel, 2))
    return bool(passed), rec


def score_events(ev_rows: list[dict], policy: dict, provider_name: str):
    pending = [r for r in ev_rows if r.get("status") == "pending"]
    if not pending:
        print("OOS v2: 성숙 대기 이벤트 없음")
        return
    sr = policy["scoring_rules"]
    earliest = min(dt.date.fromisoformat(r["asof"]) for r in pending)
    start = earliest - dt.timedelta(days=450)
    end = dt.date.today()
    universe = load_json(os.path.join(HERE, "universe.json"))
    params = load_json(os.path.join(HERE, "params.json"))
    provider = get_provider(provider_name)
    prices = {s["ticker"]: provider.get_stock_ohlcv(s["ticker"], start, end) for s in universe["stocks"]}
    kospi = provider.get_index_ohlcv(universe["benchmark"]["index"], start, end)
    ok, bad, _, _ = check_coverage(prices, universe, start, require_all=True)
    if not ok:
        print("OOS v2 채점 보류: coverage 문제: " + " / ".join(bad))
        return
    prep = prepare(universe, prices, kospi, params, start=start)
    dates = prep["dates"]
    x = prep["ew_index"]
    d2i = {d.date().isoformat(): i for i, d in enumerate(dates)}
    floor = float(sr["fwd120_floor_pct"])

    for r in pending:
        i = d2i.get(r["asof"])
        if i is None:
            r["status"] = "date_not_found"
            continue
        if r["scoring"] not in MATURITY:
            r["status"] = "unknown_scoring"
            continue
        si = subject_index(prep, r["subject"]) if r["scoring"].startswith("rel_") else None
        passed, rec = event_outcome(x, i, r["scoring"], floor, si)
        if passed is None:
            if rec["status"] != "pending":
                r["status"] = rec["status"]
            continue
        w = MATURITY[r["scoring"]]
        r.update({k: str(v) for k, v in rec.items()})
        r.update(status="scored", **{"pass": str(bool(passed))},
                 maturity_asof=dates[i + w].date().isoformat())
        print(f"OOS v2 채점 {r['asof']} {r['kind']}({r['subject']}) {r['scoring']}: pass={passed}")


def gate_status(ev_rows: list[dict]) -> str:
    scored = [r for r in ev_rows if r.get("status") == "scored"]
    by_dir = {}
    for r in scored:
        d = "bull" if r["scoring"] in ("basket_bull", "rel_outperform") else "bear"
        by_dir[d] = by_dir.get(d, 0) + 1
    return f"채점완료 {len(scored)}개 (상승측 {by_dir.get('bull', 0)} / 하락측 {by_dir.get('bear', 0)})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="naver")
    ap.add_argument("--outdir", default=os.path.join(HERE, "output"))
    args = ap.parse_args()

    policy = load_json(os.path.join(HERE, POLICY_FILE))
    if not policy or policy.get("status") != "FROZEN_OOS_POLICY_V2":
        raise SystemExit("OOS policy v2 missing or not frozen")

    latest = load_json(os.path.join(args.outdir, "latest.json"), {}) or {}
    hist_path = os.path.join(args.outdir, "history.csv")
    ev_path = os.path.join(args.outdir, "oos_events.csv")

    rows = read_csv(hist_path)
    retire_v1(rows, policy)
    merge_history(rows, latest)
    rows.sort(key=lambda r: r.get("asof", ""))
    write_csv(hist_path, rows, HIST_FIELDS)

    ev_rows = read_csv(ev_path)
    merge_events(ev_rows, latest, policy)
    score_events(ev_rows, policy, args.provider)
    ev_rows.sort(key=lambda r: (r.get("asof", ""), r.get("kind", "")))
    write_csv(ev_path, ev_rows, EV_FIELDS)
    print("OOS v2 게이트: " + gate_status(ev_rows))


if __name__ == "__main__":
    main()
