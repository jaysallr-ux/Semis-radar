"""시점별 유니버스 상태 타임라인 (사전등록 2026-11-06 08:10 유효기간 규칙)

입력
  classification/classification.csv   공시별 판정 (같은 code·basis_rcept_no 는 마지막 행이 유효)
  classification/b_search_log.csv, c_search_log.csv   중간 공시에서 찾은 반도체 비중 (found=Y)
  classification/structure_events.csv  합병·분할 등 구조 변경 (code, effective, material, recheck_share, note)
출력
  classification/universe_timeline.csv  code, start, end(미포함), verdict, part, hold_class, source, rule
  classification/timeline_changes.csv   유효기간 규칙 적용 전(공시별 판정만) 대비 바뀐 구간

규칙
  1) 사업보고서(및 표본 시작용 FY2013 사업보고서)·분할 구조 행은 기준 상태(baseline). 다음 기준 행까지 유지
  2) 상장·분할 투자설명서 행(P1-L)과 중간 공시 수치(found=Y)는 덮어쓰기 상태(overlay):
     공개일부터 다음 사업보고서 공개 직전까지 유효. 그 사이 다른 overlay 가 나오면 갱신
  3) 사업보고서 자체에서 수치가 확인되면(found=Y, 사업보고서) 그 보고서의 기준 상태를 반도체로 대체
  4) 수치 30% 미만이면 그 공개일부터 '제외'
  5) material 구조 변경일에 overlay 가 살아 있으면 recheck_share 로 재검토: 30% 이상이면 유지, 아니면 기준 상태(B/C)로
"""
import os

import pandas as pd

CLS = os.path.join(os.path.dirname(__file__), "classification")
END = "20261231"


def is_annual(rep: str) -> bool:
    r = str(rep)
    return r.startswith("사업보고서") or r.startswith("[첨부추가]사업보고서") or "주요사항보고서(회사분할결정)" in r


def pct(v):
    try:
        return float(str(v).rstrip("%"))
    except ValueError:
        return None


def load():
    c = pd.read_csv(os.path.join(CLS, "classification.csv"), dtype=str).fillna("")
    latest = c.groupby(["code", "basis_rcept_no"]).tail(1).copy()
    logs = []
    for fn in ("b_search_log.csv", "c_search_log.csv"):
        p = os.path.join(CLS, fn)
        if os.path.exists(p):
            logs.append(pd.read_csv(p, dtype=str).fillna(""))
    lg = pd.concat(logs) if logs else pd.DataFrame(columns=["code", "rcept_no", "rcept_dt", "report_nm", "found", "value"])
    sp = os.path.join(CLS, "structure_events.csv")
    se = pd.read_csv(sp, dtype=str).fillna("") if os.path.exists(sp) else pd.DataFrame(columns=["code", "effective", "material", "recheck_share", "note"])
    return latest, lg[lg.found == "Y"], se


def segments_for(code, rows, figs, sev, all_base=False):
    rows = rows.sort_values("effective_from")
    part_default = next((p for p in rows.part[::-1] if p), "")
    base = [(r.effective_from, r.verdict, r.part, r.hold_class, f"{r.review_id}:{r.basis_rcept_no}") for r in rows.itertuples() if all_base or is_annual(r.basis_report)]
    annual_dates = sorted({d for d, *_ in base})
    over = [(r.effective_from, r.verdict, r.part or part_default, r.hold_class, f"{r.review_id}:{r.basis_rcept_no}") for r in rows.itertuples() if not all_base and not is_annual(r.basis_report)]
    for f in figs.itertuples():
        v = pct(f.value)
        st = ("반도체", part_default, "") if v is not None and v >= 30 else ("제외", "", "")
        src = f"수치 {f.value}:{f.rcept_no}"
        if str(f.report_nm).startswith("사업보고서"):
            base.append((f.rcept_dt, *st, src))
            annual_dates = sorted(set(annual_dates) | {f.rcept_dt})
        else:
            over.append((f.rcept_dt, *st, src))
    # 같은 날짜 기준 행은 수치(src '수치')를 우선
    base.sort(key=lambda x: (x[0], 0 if x[4].startswith("수치") else -1))
    dates = sorted({d for d, *_ in base} | {d for d, *_ in over} | set(sev.effective) | {END})
    out, cur_base, cur_over = [], None, None
    for i, d in enumerate(dates[:-1]):
        for b in [b for b in base if b[0] == d]:
            cur_base = b
        if any(b[0] == d for b in base):
            cur_over = None  # 사업보고서 공개일에 overlay 만료
        for o in [o for o in over if o[0] == d]:
            nxt = next((a for a in annual_dates if a > d), END)
            cur_over = (*o, nxt)
        for s in sev[sev.effective == d].itertuples():
            if s.material == "Y" and cur_over:
                v = pct(s.recheck_share)
                if v is None or v < 30:
                    cur_over = None
        if cur_over and d >= cur_over[5]:
            cur_over = None
        st = cur_over if cur_over else cur_base
        if st is None:
            continue
        rule = "overlay(다음 사업보고서 전까지)" if cur_over else "baseline"
        out.append({"code": code, "start": d, "end": dates[i + 1], "verdict": st[1], "part": st[2], "hold_class": st[3], "source": st[4], "rule": rule})
    # 인접 동일 상태 병합
    m = []
    for s in out:
        if m and all(m[-1][k] == s[k] for k in ("verdict", "part", "hold_class", "source", "rule")) and m[-1]["end"] == s["start"]:
            m[-1]["end"] = s["end"]
        else:
            m.append(s)
    return m


def main():
    latest, figs, se = load()
    tl, base_only = [], []
    for code, rows in latest.groupby("code"):
        tl += segments_for(code, rows, figs[figs.code == code], se[se.code == code])
        base_only += segments_for(code, rows, figs.iloc[0:0], se.iloc[0:0], all_base=True)
    t = pd.DataFrame(tl)
    t.to_csv(os.path.join(CLS, "universe_timeline.csv"), index=False, encoding="utf-8-sig")
    b = pd.DataFrame(base_only)
    # 변경 구간: 같은 code 의 상태가 다른 구간
    ch = []
    for code, g in t.groupby("code"):
        for s in g.itertuples():
            prev = b[(b.code == code) & (b.start <= s.start) & (b.end > s.start)]
            pv = prev.iloc[-1] if len(prev) else None
            key_new = (s.verdict, s.hold_class)
            key_old = (pv.verdict, pv.hold_class) if pv is not None else ("(없음)", "")
            if key_new != key_old:
                ch.append({"code": code, "start": s.start, "end": s.end, "before": "/".join(key_old).strip("/"), "after": "/".join(key_new).strip("/"), "source": s.source, "rule": s.rule})
    pd.DataFrame(ch).to_csv(os.path.join(CLS, "timeline_changes.csv"), index=False, encoding="utf-8-sig")
    print("타임라인", len(t), "구간 |", t.code.nunique(), "개사 | 변경 구간", len(ch))


if __name__ == "__main__":
    main()
