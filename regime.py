"""
레짐 판정 엔진. 일일 실행과 백테스트가 같은 함수를 쓴다.

핵심 원칙
  - 기본 RS 기준: 동료 평균(peer avg) = 그날 유효한 28종목 20일 수익률의 단순 평균 (하닉 대비 아님)
    ※ 백테스트의 ew_index(일별 동일가중 누적지수)와는 다른 개념
  - 보조 RS 기준: KOSPI (못 받으면 해당 조건은 건너뜀)
  - 하닉은 기준축이 아니라 '후보 주도주' → 바스켓 대비 RS로 하닉 본체 주도 여부 별도 판정
  - 모든 상태는 발동/해제 조건이 다른 히스테리시스
  - 상태는 매일 전체 기간을 처음부터 다시 돌려 계산 → 상태 파일이 없어도 결과가 재현됨

v2 (2026-11-05) 논리 수정. 단계(phase) 판정은 v1과 동일 → 동결 파라미터·백테스트 v3 단계 결과 불변.
  - 하닉 본체 주도: RS만으로 켜지던 것 → 하닉 추세 ON 필수. 추세 OFF + RS 발동선 이상은
    '상대 방어'(hynix_defensive)로만 기록하고 알림 대상 아님 (하락장에서 덜 빠진 걸 주도로 오인하던 문제)
  - 주도 파트: 해제 조건에 파트 추세 ON 비율 하한(part_lead_exit_on_share) 추가
    (시장 전체가 죽었는데 RS만 남아 리더가 유지되던 문제)
  - 후반 경고: 사이클확인이 아니면 '해당없음'(평시 '없음'과 구분 → 사이클 이탈이 경고 해제로 읽히지 않게)
  - 주도 RS 고점하락: 리더가 바뀌어도 그 리더의 과거 20일 RS를 데이터에서 바로 계산 (리셋 없음)
    리더가 사라지면 사이클확인 구간의 마지막 리더로 계속 평가
  - describe_change 가 방향이 붙은 구조화 이벤트(events)를 같이 돌려줌 → OOS 채점은 이벤트 기준
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from data_provider import expected_from
from indicators import compute_indicators

ENGINE_VERSION = "v2"
PHASE_KR = {"NORMAL": "평시", "EARLY": "초입후보", "CONFIRMED": "사이클확인", "HOLD": "판정보류"}
PHASE_RANK = {"HOLD": -1, "NORMAL": 0, "EARLY": 1, "CONFIRMED": 2}
LATE_KR = {0: "없음", 1: "없음", 2: "주의", 3: "후반경고"}
LATE_NA = "해당없음"
LATE_RANK = {"없음": 0, "주의": 1, "후반경고": 2, "강한 후반경고": 3}


def _b(series) -> np.ndarray:
    return series.astype("boolean").fillna(False).to_numpy(dtype=bool)


def late_level(score: int) -> str:
    return "강한 후반경고" if score >= 4 else LATE_KR.get(score, "없음")


def update_part_leader(st: dict, part_stats: dict, p: dict):
    """
    st: {"leader", "cand", "count"} 를 제자리 갱신하고 현재 주도 파트를 돌려준다.
      - 현 리더 RS >= 해제선 AND 파트 추세 ON 비율 >= part_lead_exit_on_share 이면 유지
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
    if (cur and cur in part_stats
            and part_stats[cur]["median_rs20"] >= p["part_lead_exit_rs"]
            and part_stats[cur]["on_share"] >= p["part_lead_exit_on_share"]):
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


def prepare(universe: dict, prices: dict, kospi: pd.DataFrame | None, p: dict, start=None) -> dict:
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
    # expected: 그날 데이터가 '있어야 정상'인 종목.
    # 실제 첫 수신일이 아니라 선언된 기대 시작일(list_date/valid_from/요청 시작일)로 정한다.
    # → 소스가 과거를 잘라도 '상장 전'으로 오인하지 않고 결측으로 잡힘
    start_d = start or master[0].date()
    expected = np.zeros((T, N), dtype=bool)
    for j, s in enumerate(stocks):
        expected[:, j] = master >= pd.Timestamp(expected_from(s, start_d))

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

    rs20p = (arr["ret20"] - peer20[:, None]) * 100
    parts_arr = np.array([s["part"] for s in stocks])
    semi_arr = parts_arr != "대형"
    part_order = list(dict.fromkeys(s["part"] for s in stocks if s["part"] != "대형"))
    part_med_rs20 = {}
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for pn in part_order:
            m = valid & (semi_arr & (parts_arr == pn))[None, :]
            part_med_rs20[pn] = np.nanmedian(np.where(m, rs20p, np.nan), axis=1)

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
        "rs20p": rs20p,
        "part_med_rs20": part_med_rs20,
        "rs65p": (arr["ret65"] - peer65[:, None]) * 100,
        "rs20k": (arr["ret20"] - kospi20[:, None]) * 100,
        "last_date": last_date,
        "part_order": part_order,
    }


def subject_index(prep: dict, subject: str) -> np.ndarray | None:
    """하닉 = 종가, 파트 = 유효 종목 일간수익률 동일가중 누적지수 (OOS 채점·주도 파트 검증 공용)"""
    if subject == "hynix":
        return prep["arr"]["close"][:, prep["idx_hynix"]]
    m = prep["parts"] == subject
    if not m.any():
        return None
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        dr = np.where(prep["valid"][:, m], prep["arr"]["daily_ret"][:, m], np.nan)
        daily = np.nan_to_num(np.nanmean(dr, axis=1))
    return np.cumprod(1 + daily)


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
    last_lead_key = None  # 사이클확인 구간에서 마지막으로 잡힌 리더 (리더가 사라져도 후반 경고 평가 유지)
    L = p["late_rs_lookback"]
    out = []

    def lead_series(key, t):
        if key == "hynix":
            s = prep["rs20p"][max(0, t - L + 1):t + 1, hy]
        else:
            s = prep["part_med_rs20"][key][max(0, t - L + 1):t + 1]
        return s[~np.isnan(s)]

    for t in range(T):
        v = prep["valid"][t]
        vs = v & semi
        n = int(vs.sum())
        rec = {"date": dates[t].date().isoformat(), "n_semis": n}
        if t < warmup_skip or n < p["min_semis_valid"] or not v[hy]:
            rec.update({"phase": "HOLD", "hynix_lead": False, "hynix_defensive": False, "part_leader": None,
                        "active_chains": [], "late_score": 0, "late_level": LATE_NA})
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

        # ---- 하닉 본체 주도 (v2: 추세 ON 필수) ----
        rs_ok = hy_rs20p >= (p["hynix_lead_exit_rs"] if hynix_lead else p["hynix_lead_enter_rs"])
        hynix_lead = bool(rs_ok and hy_trend)
        hynix_defensive = bool(hy_rs20p >= p["hynix_lead_enter_rs"] and not hy_trend)

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
        items = {}
        if phase == "CONFIRMED":
            key = "hynix" if hynix_lead else part_leader
            if key is not None:
                last_lead_key = key
            else:
                key = last_lead_key  # 리더 상실 → 마지막 리더 기준으로 계속 평가
            lm = vs & late_mask
            items["후행파트_확산"] = bool(lm.sum() > 0 and (tr & lm).sum() / lm.sum() >= p["late_parts_on_share"])
            hist = lead_series(key, t) if key is not None else np.array([])
            if len(hist) >= 5:
                items["주도_RS_고점하락"] = bool(hist[-1] - hist.max() <= -p["late_rs_drop"])
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
        else:
            last_lead_key = None
        score = int(sum(items.values()))
        level = late_level(score) if phase == "CONFIRMED" else LATE_NA

        rec.update({
            "phase": phase,
            "hynix_lead": bool(hynix_lead),
            "hynix_defensive": hynix_defensive,
            "part_leader": part_leader,
            "active_chains": sorted(active_chains),
            "late_score": score, "late_level": level, "late_items": items,
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


def _ev(kind, msg, scoring=None, subject=None):
    """scoring: basket_bull / basket_bear / rel_outperform / rel_underperform / None(채점 안 함)"""
    return {"kind": kind, "msg": msg, "scoring": scoring, "subject": subject}


def describe_change(prev: dict | None, cur: dict) -> tuple[bool, list[str], list[dict]]:
    """상태 변화 → (changed, 메시지 목록, 구조화 이벤트 목록). 이벤트 방향은 OOS 채점에 쓴다."""
    if prev is None:
        return False, [], []
    ev = []
    pp, cp = prev["phase"], cur["phase"]
    if pp != cp:
        up = PHASE_RANK.get(cp, -1) > PHASE_RANK.get(pp, -1)
        scoring = None if "HOLD" in (pp, cp) else ("basket_bull" if up else "basket_bear")
        ev.append(_ev("phase_change", f"단계: {PHASE_KR[pp]} → {PHASE_KR[cp]}", scoring))
    if bool(prev.get("hynix_lead")) != bool(cur.get("hynix_lead")):
        if cur.get("hynix_lead"):
            ev.append(_ev("leader_start", "SK하이닉스 본체 주도 시작", "rel_outperform", "hynix"))
        else:
            ev.append(_ev("leader_end", "SK하이닉스 본체 주도 해제", "rel_underperform", "hynix"))
    pl, cl = prev.get("part_leader"), cur.get("part_leader")
    if pl != cl:
        msg = f"주도 파트: {pl or '없음'} → {cl or '없음'}"
        if pl:
            ev.append(_ev("leader_end", msg, "rel_underperform", pl))
        if cl:
            ev.append(_ev("leader_start", msg, "rel_outperform", cl))
    added = set(cur["active_chains"]) - set(prev["active_chains"])
    removed = set(prev["active_chains"]) - set(cur["active_chains"])
    for ch in sorted(added):
        ev.append(_ev("chain_change", f"{ch} 체인 활성화 (순환매 확산 후보)", None, ch))
    for ch in sorted(removed):
        ev.append(_ev("chain_change", f"{ch} 체인 비활성화", None, ch))
    plv, clv = prev.get("late_level"), cur.get("late_level")
    # 사이클확인 진입/이탈로 생기는 '해당없음' 전환은 단계 메시지가 이미 설명 → 별도 알림 안 함
    if plv != clv and LATE_NA not in (plv, clv) and plv in LATE_RANK and clv in LATE_RANK:
        up = LATE_RANK[clv] > LATE_RANK[plv]
        ev.append(_ev("late_change", f"후반 경고: {plv} → {clv}", "basket_bear" if up else None))
    msgs = list(dict.fromkeys(e["msg"] for e in ev))
    return bool(ev), msgs, ev
