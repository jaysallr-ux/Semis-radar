"""
매일 장 마감 후 실행.
  python run_daily.py --provider naver
  python run_daily.py --provider fake      # 네트워크 없이 동작 확인용

결과: output/latest.json (GPT가 읽는 유일한 파일), output/history.csv (상태 변화 기록)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import numpy as np

from data_provider import check_coverage, get_provider
from regime import PHASE_KR, describe_change, prepare, run_engine

KST = ZoneInfo("Asia/Seoul")
HERE = os.path.dirname(os.path.abspath(__file__))


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def memory_flag(path: str, p: dict, today: dt.date):
    """memory_price.json 수동 입력값. 오래됐거나 없으면 None (후반 경고에서 제외)."""
    m = load_json(path)
    if not m or not m.get("updated") or m.get("momentum") not in ("up", "flat", "down"):
        return None, "미입력"
    age = (today - dt.date.fromisoformat(m["updated"])).days
    if age > p["memory_stale_days"]:
        return None, f"오래됨({age}일)"
    return m["momentum"] == "down", m["momentum"]


def r2(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="naver")
    ap.add_argument("--outdir", default=os.path.join(HERE, "output"))
    ap.add_argument("--lookback-days", type=int, default=900, help="달력일 기준 수신 기간")
    ap.add_argument("--allow-intraday", action="store_true", help="장중 진단용. 생산 스케줄에서는 사용 금지")
    args = ap.parse_args()

    universe = load_json(os.path.join(HERE, "universe.json"))
    p = load_json(os.path.join(HERE, "params.json"))

    now = dt.datetime.now(KST)
    # 장 마감 확정 전 실행 차단. 수능일처럼 마감이 늦는 날은 params.late_close_dates 에 등록
    late = now.date().isoformat() in p.get("late_close_dates", [])
    cutoff = dt.time(16, 40) if late else dt.time(15, 40)
    if (args.provider.lower() != "fake" and not args.allow_intraday
            and now.weekday() < 5 and now.time() < cutoff):
        print(f"[SKIP] {now:%H:%M} KST — 종가 확정 전({cutoff:%H:%M} 이전)이라 판정/저장하지 않음")
        return
    os.makedirs(args.outdir, exist_ok=True)
    latest_path = os.path.join(args.outdir, "latest.json")
    prev_out = load_json(latest_path) or {}
    prev_ok_state = prev_out.get("last_ok_state")
    prev_err_sig = prev_out.get("error_signature")

    today = now.date()
    start = today - dt.timedelta(days=args.lookback_days)
    provider = get_provider(args.provider)

    prices, errors = {}, {}
    for s in universe["stocks"]:
        try:
            prices[s["ticker"]] = provider.get_stock_ohlcv(s["ticker"], start, today)
        except Exception as e:
            errors[s["ticker"]] = str(e)
    kospi = provider.get_index_ohlcv(universe["benchmark"]["index"], start, today)

    result = {
        "schema": "semis-radar/v2",
        "generated_at": now.isoformat(timespec="seconds"),
        "provider": provider.name,
        "data_ok": False, "asof": None, "is_new_bar": False,
        "missing": [], "fetch_errors": errors,
        "coverage": {},
        "last_ok_state": prev_ok_state, "error_signature": None,
        "alert": {"changed": False, "message": ""},
    }

    def write():
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    def fail(problems: list[str]):
        sig = sorted(problems)
        result["data_ok"] = False
        result["error_signature"] = sig
        result["state"] = {"phase": "HOLD", "phase_kr": PHASE_KR["HOLD"]}
        changed = sig != prev_err_sig
        result["alert"] = {"changed": changed, "message": "[데이터 오류] 판정 보류: " + " / ".join(sig)}
        write()
        print(f"판정 보류: {sig} / alert.changed={changed}")

    bm = universe["benchmark"]
    cov_ok, cov_bad, cov_report, _ = check_coverage(prices, universe, start)
    result["coverage"] = {"requested_start": start.isoformat(), "stocks": cov_report}
    if not cov_ok:
        return fail([f"과거 데이터 부족 {b}" for b in cov_bad])

    try:
        prep = prepare(universe, prices, kospi, p, start=start)
    except Exception as e:
        return fail([f"판정 불가 {e}"])

    asof = prep["dates"][-1]
    result["asof"] = asof.date().isoformat()
    is_new_bar = prev_out.get("asof") != result["asof"]
    result["is_new_bar"] = is_new_bar

    missing, missing_tickers = [], set()
    for j, s in enumerate(universe["stocks"]):
        ld = prep["last_date"].get(s["ticker"])
        if ld is None or ld < asof:
            missing.append(s["name"])
            missing_tickers.add(s["ticker"])
            prep["valid"][-1, j] = False
            prep["trend"][-1, j] = False
            prep["new_turn"][-1, j] = False
    result["missing"] = missing
    valid_share = 1 - len(missing) / len(universe["stocks"])
    if bm["hynix"] in missing_tickers or valid_share < p["min_valid_share"]:
        return fail([f"결측 {len(missing)}종목: {', '.join(missing)}"])

    result["data_ok"] = True
    mem_down, mem_status = memory_flag(os.path.join(HERE, "memory_price.json"), p, today)
    days = run_engine(prep, p, memory_down=mem_down)
    cur = days[-1]
    cur_state = {k: cur.get(k) for k in ["phase", "hynix_lead", "part_leader", "active_chains", "late_level"]}

    rows = []
    a = prep["arr"]
    for j, s in enumerate(universe["stocks"]):
        on_idx = np.where(prep["turn_on"][:, j])[0]
        nt_idx = np.where(prep["new_turn"][:, j])[0]
        rows.append({
            "ticker": s["ticker"], "name": s["name"], "part": s["part"], "chain": s.get("chain", "미확인"),
            "valid": bool(prep["valid"][-1, j]),
            "close": r2(a["close"][-1, j], 0),
            "ret20_pct": r2(a["ret20"][-1, j] * 100), "ret65_pct": r2(a["ret65"][-1, j] * 100),
            "rs20_vs_peers": r2(prep["rs20p"][-1, j]), "rs65_vs_peers": r2(prep["rs65p"][-1, j]),
            "rs20_vs_kospi": r2(prep["rs20k"][-1, j]),
            "dist_52w_high_pct": r2(a["dist52"][-1, j] * 100),
            "vol_ratio": r2(a["vol_ratio"][-1, j]),
            "ma20": r2(a["ma20"][-1, j], 0), "ma20_slope5_pct": r2(a["ma20_slope5"][-1, j] * 100),
            "trend": "ON" if prep["trend"][-1, j] else "OFF",
            "last_turn_on": prep["dates"][on_idx[-1]].date().isoformat() if len(on_idx) else None,
            "last_new_turn": prep["dates"][nt_idx[-1]].date().isoformat() if len(nt_idx) else None,
        })

    if prev_err_sig:
        _, diff = describe_change(prev_ok_state, cur_state) if prev_ok_state else (False, [])
        changed, msgs = True, ["[데이터 정상화]"] + diff
    elif not is_new_bar:
        changed, msgs = False, []
    elif prev_ok_state is None:
        changed, msgs = False, ["[기준선 생성] 첫 정상 실행이라 알림 없음"]
    else:
        changed, msgs = describe_change(prev_ok_state, cur_state)
    result["last_ok_state"] = cur_state

    leader_txt = ("SK하이닉스 본체" if cur.get("hynix_lead") else "") + \
                 ((" + " if cur.get("hynix_lead") and cur.get("part_leader") else "") + (cur.get("part_leader") or ""))
    result.update({
        "is_new_bar": is_new_bar,
        "state": {
            "phase": cur["phase"], "phase_kr": PHASE_KR[cur["phase"]],
            "leader": leader_txt or "없음",
            "hynix_lead": cur.get("hynix_lead"), "part_leader": cur.get("part_leader"),
            "active_chains": cur.get("active_chains", []),
            "late_score": cur.get("late_score", 0), "late_level": cur.get("late_level", "없음"),
            "late_items": cur.get("late_items", {}),
            "memory_price_input": mem_status,
        },
        "market": {k: cur.get(k) for k in ["n_semis", "breadth_early", "parts_active", "on_share",
                                            "parts_majority_on", "vol_share", "kospi_rs_pos_share",
                                            "peer_avg_ret20_pct", "kospi_ret20_pct", "hynix"]},
        "parts": cur.get("parts", {}), "parts_excluded": cur.get("parts_excluded", []),
        "chains": cur.get("chains", {}),
        "stocks": rows,
        "alert": {"changed": changed, "message": " / ".join(msgs)},
    })
    write()

    if is_new_bar:
        hist_path = os.path.join(args.outdir, "history.csv")
        new_file = not os.path.exists(hist_path)
        with open(hist_path, "a", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            if new_file:
                wr.writerow(["asof", "phase", "leader", "active_chains", "late_level",
                             "breadth_early", "on_share", "data_ok", "alert"])
            wr.writerow([result["asof"], cur["phase"], leader_txt or "없음", "|".join(cur.get("active_chains", [])),
                         cur.get("late_level"), cur.get("breadth_early"), cur.get("on_share"),
                         result["data_ok"], result["alert"]["message"]])

    print(f"asof={result['asof']} data_ok={result['data_ok']} phase={cur['phase']} "
          f"leader={leader_txt or '없음'} late={cur.get('late_level')} alert={result['alert']}")


if __name__ == "__main__":
    main()
