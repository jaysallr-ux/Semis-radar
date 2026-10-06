"""
레짐 판정 엔진. 일일 실행과 백테스트가 같은 함수를 쓴다.

핵심 원칙
  - 기본 RS 기준: 동료 평균(peer avg) = 그날 유효한 28종목 20일 수익률의 단순 평균 (하닉 대비 아님)
    ※ 백테스트의 ew_index(일별 동일가중 누적지수)와는 다른 개념
  - 보조 RS 기준: KOSPI (못 받으면 해당 조건은 건너뜀)
  - 하닉은 기준축이 아니라 '후보 주도주' → 바스켓 대비 RS로 하닉 본체 주도 여부 별도 판정
  - 모든 상태는 발동/해제 조건이 다른 히스테리시스
  - 상태는 매일 전체 기간을 처음부터 다시 돌려 계산 → 상태 파일이 없어도 결과가 재현됨
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from indicators import compute_indicators

PHASE_KR = {"NORMAL": "평시", "EARLY": "초입후보", "CONFIRMED": "사이클확인", "HOLD": "판정보류"}
LATE_KR = {0: "없음", 1: "없음", 2: "주의", 3: "후반경고"}


def _b(series) -> np.ndarray:
    return series.astype("boolean").fillna(False).to_numpy(dtype=bool)


def late_level(score: int) -> str:
    return "강한 후반경고" if score >= 4 else LATE_KR.get(score, "없음")


def update_part_leader(st: dict, part_stats: dict, p: dict):
    """
    st: {"leader", "cand", "count"} 를 제자리 갱신하고 현재 주도 파트를 돌려준다.
      - 현 리더 RS >= 해제선이면 유지
      - 단, 진입조건을 다 만족하는 다른 후보가 현 리더보다 switch_margin 이상 강한 상태가
        switch_days 거래일 연속이면 교체
      - 현 리더가 해제선 밑이거나 데이터가 없으면 해제 후 최강 후보 선택
    """
    def qualifies(s):
        return (s["median_rs20"] >= p["part_lead_enter_rs"]
                and s["on_share"] >= p["part_lead_min_on_share"]
                and s["vol_share"] >= p["part_lead_min_vol_share"])

    cands = sorted(((s["median_rs20"], pn) for pn, s in part_stats.items() if qualifies(s)), reverse=True)
    cur = st["leader"]
    if cur and cur in part_stats and part_stats[cur]["median_rs20"] >= p["part_lead_exit_rs"]:
        challengers = [(rs, pn) for rs, pn in cands if pn != cur]
        if challengers and challengers[0][0] >= part_stats[cur]["median_rs20"] + p["part_lead_switch_margin"]:
            best = challengers[0][1]
            st["count"] = st["count"] + 1 if st["cand"] == best else 1
            st["cand"] = best
            if st["count"] >= p["part_lead_switch_days"]:
                st.update(leader=best, cand=None, count=0)
        else:
            st.update(cand=None, count=0)
    else:
        st.update(leader=cands[0][1] if cands else None, cand=None, count=0)
    return st["leader"]


def prepare(universe: dict, prices: dict, kospi: pd.DataFrame | None, p: dict) -> dict:
    stocks = universe["stocks"]
    hy = universe["benchmark"]["hynix"]
    if hy not in prices or prices[hy].empty:
        raise RuntimeError("SK하이닉스 데이터 없음 → 판정 불가")
    master = prices[hy].index

    tickers = [s["ticker"] for s in stocks]
    T, N = len(master), len(tickers)
    keys = ["close", "ma20", "ma20_slope5", "ret20", "ret65", "dist52", "vol_ratio", "daily_ret"]
    arr = {k: np.full((T, N), np.nan) for k in keys}
    valid = np.zeros((T, N), dtype=bool)
    trend = np.zeros((T, N), dtype=bool)
    new_turn = np.zeros((T, N), dtype=bool)
    turn_on = np.zeros((T, N), dtype=bool)
    last_date, first_date = {}, {}
    # expected: 그날 데이터가 '있어야 정상'인 종목. 상장 전/valid_from 전은 False,
    # 수신 자체가 실패한 종목은 전 구간 True (→ 결측으로 잡혀 파트 커버리지에 반영)
    expected = np.ones((T, N), dtype=bool)

    for j, s in enumerate(stocks):
        df = prices.get(s["ticker"])
        if df is None or df.empty:
            continue
        if s.get("valid_from"):
            # 이전 사업구조 데이터를 지표·상태머신 계산 전에 잘라냄 (상태가 넘어오지 않게)
            df = df.loc[pd.Timestamp(s["valid_from"]):]
            if df.empty:
                continue
        last_date[s["ticker"]] = df.index.max()
        first_date[s["ticker"]] = df.index.min()
        expected[:, j] = master >= df.index.min()
        ind = compute_indicators(df, p).reindex(master)
        for k in keys:
            arr[k][:, j] = ind[k].to_numpy(dtype=float)
        valid[:, j] = _b(ind["valid"])
        trend[:, j] = _b(ind["trend"]) & valid[:, j]
        new_turn[:, j] = _b(ind["new_turn"]) & valid[:, j]
        turn_on[:, j] = _b(ind["turn_on"]) & valid[:, j]

    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        r20 = np.where(valid, arr["ret20"], np.nan)
        r65 = np.where(valid, arr["ret65"], np.nan)
        peer20 = np.nanmean(r20, axis=1)
        peer65 = np.nanmean(r65, axis=1)
        dr = np.where(valid, arr["daily_ret"], np.nan)
        ew_daily = np.nan_to_num(np.nanmean(dr, axis=1))
    ew_index = np.cumprod(1 + ew_daily)

    if kospi is not None and not kospi.empty:
        kc = kospi["close"].reindex(master)
        kospi20 = (kc / kc.shift(p["ret_short"]) - 1).to_numpy()
        kospi65 = (kc / kc.shift(p["ret_long"]) - 1).to_numpy()
    else:
        kospi20 = np.full(T, np.nan)
        kospi65 = np.full(T, np.nan)

    return {
        "dates": master, "tickers": tickers,
        "names": np.array([s["name"] for s in stocks]),
        "parts": np.array([s["part"] for s in stocks]),
        "chains": np.array([s.get("chain", "미확인") for s in stocks]),
        "is_semi": np.array([s["part"] != "대형" for s in stocks]),
        "idx_hynix": tickers.index(hy),
        "arr": arr, "valid": valid, "expected": expected, "trend": trend, "new_turn": new_turn, "turn_on": turn_on,
        "peer20": peer20, "peer65": peer65, "ew_index": ew_index,
        "kospi20": kospi20, "kospi65": kospi65,
        "rs20p": (arr["ret20"] - peer20[:, None]) * 100,
        "rs65p": (arr["ret65"] - peer65[:, None]) * 100,
        "rs20k": (arr["ret20"] - kospi20[:, None]) * 100,
        "last_date": last_date,
        "part_order": list(dict.fromkeys(s["part"] for s in stocks if s["part"] != "대형")),
    }


def run_engine(prep: dict, p: dict, memory_down: bool | None = None, warmup_skip: int = 0) -> list[dict]:
    """일자별 판정 결과 리스트. memory_down: 메모리 가격 모멘텀 둔화 여부(수동 입력, 없으면 None)"""
    dates = prep["dates"]
    T, N = prep["valid"].shape
    w = p["new_turn_window"]
    cs = np.cumsum(prep["new_turn"].astype(int), axis=0)
    recent = cs.copy()
    recent[w:] = cs[w:] - cs[:-w]
    recent = recent > 0

    parts, chains, semi = prep["parts"], prep["chains"], prep["is_semi"]
    late_mask = np.isin(parts, p["late_parts"])
    hy = prep["idx_hynix"]
    chain_names = [c for c in dict.fromkeys(chains) if c != "미확인"]

    phase, hynix_lead, part_leader = "NORMAL", False, None
    leader_state = {"leader": None, "cand": None, "count": 0}
    active_chains: set = set()
    leader_key, leader_hist = None, []
    out = []

    for t in range(T):
        v = prep["valid"][t]
        vs = v & semi
        n = int(vs.sum())
        rec = {"date": dates[t].date().isoformat(), "n_semis": n}
        if t < warmup_skip or n < p["min_semis_valid"] or not v[hy]:
            rec.update({"phase": "HOLD", "hynix_lead": False, "part_leader": None,
                        "active_chains": [], "late_score": 0, "late_level": "없음"})
            out.append(rec)
            continue

        tr = prep["trend"][t]
        rc = recent[t]
        rs20p = prep["rs20p"][t]
        rs20k = prep["rs20k"][t]
        vr = np.nan_to_num(prep["arr"]["vol_ratio"][t], nan=0.0)
        dist52 = prep["arr"]["dist52"][t]

        breadth_early = float((rc & vs).sum() / n)
        on_share = float((tr & vs).sum() / n)
        vol_share = float((vr[vs] >= p["vol_ratio_hot"]).mean())
        rk = rs20k[vs]
        kospi_pos = float(np.mean(rk[~np.isnan(rk)] > 0)) if np.any(~np.isnan(rk)) else None

        part_stats, parts_excluded = {}, []
        exp_t = prep["expected"][t]
        for pn in prep["part_order"]:
            m = vs & (parts == pn)
            k = int(m.sum())
            k_exp = int((exp_t & semi & (parts == pn)).sum())
            if k_exp == 0:
                continue
            if k < p["part_min_valid_n"] or k / k_exp < p["part_min_valid_share"]:
                parts_excluded.append(f"{pn}({k}/{k_exp})")
                continue
            part_stats[pn] = {
                "n": k,
                "on_share": round(float((tr & m).sum() / k), 3),
                "median_rs20": round(float(np.nanmedian(rs20p[m])), 2),
                "vol_share": round(float((vr[m] >= p["vol_ratio_hot"]).mean()), 3),
                "new_turns_recent": int((rc & m).sum()),
            }
        parts_majority_on = sum(1 for s in part_stats.values() if s["on_share"] >= 0.5)
        parts_active = sum(1 for s in part_stats.values() if s["new_turns_recent"] > 0)

        hy_trend = bool(tr[hy])
        hy_rs20p = float(rs20p[hy])
        hy_rs20k = float(rs20k[hy]) if not np.isnan(rs20k[hy]) else None

        # ---- 단계 판정 (히스테리시스) ----
        early_cond = (breadth_early >= p["early_enter_breadth"]
                      and parts_active >= p["early_min_parts"]
                      and vol_share >= p["early_min_vol_share"])
        # KOSPI 대비 RS는 참고지표로만 기록. 수신 여부가 판정을 바꾸지 않게 조건에서 제외.
        confirm_cond = (on_share >= p["confirm_enter_on_share"]
                        and parts_majority_on >= p["confirm_min_parts"] and hy_trend)
        if confirm_cond:
            phase = "CONFIRMED"
        elif phase == "CONFIRMED" and on_share >= p["confirm_exit_on_share"]:
            phase = "CONFIRMED"
        elif early_cond:
            phase = "EARLY"
        elif phase == "EARLY" and breadth_early >= p["early_exit_breadth"]:
            phase = "EARLY"
        else:
            phase = "NORMAL"

        # ---- 하닉 본체 주도 ----
        hynix_lead = hy_rs20p >= (p["hynix_lead_exit_rs"] if hynix_lead else p["hynix_lead_enter_rs"])

        # ---- 주도 파트 (해제 히스테리시스 + 새 강자 교체) ----
        part_leader = update_part_leader(leader_state, part_stats, p)

        # ---- 고객사 체인 ----
        chain_stats = {}
        for ch in chain_names:
            m = vs & (chains == ch)
            k = int(m.sum())
            if k < 2:
                continue
            sh = float((tr & m).sum() / k)
            chain_stats[ch] = {"n": k, "on_share": round(sh, 3)}
            if ch in active_chains:
                if sh < p["chain_exit_on_share"]:
                    active_chains.discard(ch)
            elif sh >= p["chain_enter_on_share"]:
                active_chains.add(ch)

        # ---- 후반 경고 스코어 ----
        key = "hynix" if hynix_lead else part_leader
        if key != leader_key:
            leader_key, leader_hist = key, []
        lead_metric = hy_rs20p if key == "hynix" else (part_stats[key]["median_rs20"] if key in part_stats else None)
        if lead_metric is not None:
            leader_hist = (leader_hist + [lead_metric])[-p["late_rs_lookback"]:]

        items = {}
        if phase == "CONFIRMED":
            lm = vs & late_mask
            items["후행파트_확산"] = bool(lm.sum() > 0 and (tr & lm).sum() / lm.sum() >= p["late_parts_on_share"])
            if lead_metric is not None and len(leader_hist) >= 5:
                items["주도_RS_고점하락"] = bool(lead_metric - max(leader_hist) <= -p["late_rs_drop"])
                gm = np.zeros(N, dtype=bool)
                if key == "hynix":
                    gm[hy] = True
                else:
                    gm = vs & (parts == key)
                d = dist52[gm]
                d = d[~np.isnan(d)]
                items["대장_신고가실패"] = bool(len(d) > 0 and np.mean(d <= p["late_dist52_fail"]) >= p["late_fail_share"])
            items["하닉_약화"] = bool(not hy_trend)
            if memory_down is not None:
                items["메모리가격_둔화"] = bool(memory_down)
        score = int(sum(items.values()))

        rec.update({
            "phase": phase,
            "hynix_lead": bool(hynix_lead),
            "part_leader": part_leader,
            "active_chains": sorted(active_chains),
            "late_score": score, "late_level": late_level(score), "late_items": items,
            "breadth_early": round(breadth_early, 3), "parts_active": parts_active,
            "on_share": round(on_share, 3), "parts_majority_on": parts_majority_on,
            "vol_share": round(vol_share, 3), "kospi_rs_pos_share": None if kospi_pos is None else round(kospi_pos, 3),
            "peer_avg_ret20_pct": round(float(prep["peer20"][t]) * 100, 2),
            "kospi_ret20_pct": None if np.isnan(prep["kospi20"][t]) else round(float(prep["kospi20"][t]) * 100, 2),
            "hynix": {"trend": hy_trend, "rs20_vs_peers": round(hy_rs20p, 2),
                      "rs20_vs_kospi": None if hy_rs20k is None else round(hy_rs20k, 2)},
            "parts": part_stats, "parts_excluded": parts_excluded, "chains": chain_stats,
        })
        out.append(rec)
    return out


def state_key(r: dict) -> tuple:
    return (r["phase"], r["hynix_lead"], r["part_leader"], tuple(r["active_chains"]), r["late_level"])


def describe_change(prev: dict | None, cur: dict) -> tuple[bool, list[str]]:
    if prev is None:
        return False, []
    msgs = []
    if prev["phase"] != cur["phase"]:
        msgs.append(f"단계: {PHASE_KR[prev['phase']]} → {PHASE_KR[cur['phase']]}")
    if prev["hynix_lead"] != cur["hynix_lead"]:
        msgs.append("SK하이닉스 본체 주도 " + ("시작" if cur["hynix_lead"] else "해제"))
    if prev["part_leader"] != cur["part_leader"]:
        msgs.append(f"주도 파트: {prev['part_leader'] or '없음'} → {cur['part_leader'] or '없음'}")
    added = set(cur["active_chains"]) - set(prev["active_chains"])
    removed = set(prev["active_chains"]) - set(cur["active_chains"])
    for ch in sorted(added):
        msgs.append(f"{ch} 체인 활성화 (순환매 확산 후보)")
    for ch in sorted(removed):
        msgs.append(f"{ch} 체인 비활성화")
    if prev["late_level"] != cur["late_level"]:
        msgs.append(f"후반 경고: {prev['late_level']} → {cur['late_level']}")
    return bool(msgs), msgs
