"""
검토용 발췌 (커밋 가능한 크기). 정기보고서의 「사업의 내용」에서
  - 1. 사업의 개요 앞부분
  - 2. 주요 제품(및 서비스) 전체 (매출 비중 표가 주로 여기)
를 뽑아 research/pit/classification/excerpts/<code>_<rcept_no>.txt 로 저장. 원문 SHA-256 을 머리에 적는다.

  python research/pit/dart_excerpt.py --list research/pit/classification/excerpt_request.csv
  excerpt_request.csv: code,rcept_no,rcept_dt,report_nm,reason[,grep]
  grep(선택): 정규식. 주면 원문 전체에서 일치 위치마다 앞 300자·뒤 1500자를 <code>_<rcept_no>_grep.txt 로 저장
"""
import argparse
import hashlib
import io
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dart_scan import TAG, call, decode  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classification", "excerpts")
SUB = re.compile(r'<TITLE[^>]*>([^<]*)</TITLE>', re.I)
OVERVIEW_CHARS = 2500
PRODUCT_CHARS = 6000
SALES_CHARS = 5000


def clean(x):
    x = re.sub(r"</(TR|P|TITLE)>", "\n", x, flags=re.I)
    x = re.sub(r"</T[DHE]>", " | ", x, flags=re.I)
    x = TAG.sub(" ", x)
    x = re.sub(r"[ \t　]+", " ", x)
    return re.sub(r"\n\s*\n+", "\n", x).strip()


def business_parts(xml):
    st = re.search(r'<TITLE[^>]*AASSOCNOTE="D-0-2-0-0"', xml)
    if not st:
        m = re.search(r"<TITLE[^>]*>\s*(II|Ⅱ)\s*[\.．]\s*사업의\s*내용", xml)
        if not m:
            return None, None, False, None
        s0 = m.start()
    else:
        s0 = st.start()
    en = re.search(r'<TITLE[^>]*AASSOCNOTE="D-0-[3-9]-0-0"', xml[s0 + 10:])
    body = xml[s0: s0 + 10 + en.start()] if en else xml[s0:s0 + 400000]
    titles = [(m.start(), m.group(1).strip()) for m in SUB.finditer(body)]
    def sect(pat):
        for k, (pos, t) in enumerate(titles):
            if re.search(pat, t):
                end = titles[k + 1][0] if k + 1 < len(titles) else len(body)
                return clean(body[pos:end])
        return None
    ov = sect(r"사업의\s*개요")
    pr = sect(r"주요\s*제품")
    full = clean(body)
    if pr is None:
        # 옛 서식(2015~2020년대 초)은 소제목 TITLE 없이 본문에 '주요 제품', '매출실적' 등이 들어 있음
        m = re.search(r"(주요\s*제품\s*(등의)?\s*(현황|매출)|사업부문별\s*매출|제품별\s*매출|매출\s*실적|부문별\s*매출)", full)
        if m:
            pr = "[본문 검색: " + m.group(0) + "]\n" + full[m.start():m.start() + PRODUCT_CHARS]
    sa = sect(r"매출\s*(및|에\s*관한)")
    if sa is None:
        m2 = re.search(r"(매출\s*및\s*수주\s*상황|매출에\s*관한\s*사항|매출실적|용도별\s*매출|제품별\s*매출)", full[OVERVIEW_CHARS:])
        if m2:
            st2 = OVERVIEW_CHARS + m2.start()
            sa = "[본문 검색: " + m2.group(0) + "]\n" + full[st2:st2 + SALES_CHARS]
    if ov is None and pr is None:
        return full[:OVERVIEW_CHARS + PRODUCT_CHARS], None, True, sa
    return ov if ov is not None else full[:OVERVIEW_CHARS], pr, True, sa


def one(r):
    fn = os.path.join(OUT, f"{r.code}_{r.rcept_no}.txt")
    gp = str(getattr(r, "grep", "") or "")
    gp = "" if gp == "nan" else gp
    if os.path.exists(fn) and "재추출" not in str(r.reason) and not gp:
        return
    b = call("document.xml", {"rcept_no": r.rcept_no}, binary=True)
    if b is None:
        print("실패", r.code, r.rcept_no)
        return
    z = zipfile.ZipFile(io.BytesIO(b))
    names = z.namelist()
    main_f = next((n for n in names if os.path.basename(n) == f"{r.rcept_no}.xml"), None) or \
        max(names, key=lambda n: z.getinfo(n).file_size)
    xml, enc = decode(z.read(main_f))
    ov, pr, found, sales = business_parts(xml)
    with open(fn, "w", encoding="utf-8") as f:
        f.write(f"code={r.code} rcept_no={r.rcept_no} rcept_dt={r.rcept_dt} report={r.report_nm}\n")
        f.write(f"sha256={hashlib.sha256(b).hexdigest()} encoding={enc} section_found={int(found)} reason={r.reason}\n")
        f.write("=== 사업의 개요 (앞부분) ===\n")
        f.write((ov or "(없음)")[:OVERVIEW_CHARS] + "\n")
        f.write("=== 주요 제품 ===\n")
        f.write((pr or "(없음)")[:PRODUCT_CHARS] + "\n")
        f.write("=== 매출 및 수주상황 ===\n")
        f.write((sales or "(없음)")[:SALES_CHARS] + "\n")
    if gp:
        full = clean(xml)
        gfn = os.path.join(OUT, f"{r.code}_{r.rcept_no}_grep.txt")
        with open(gfn, "w", encoding="utf-8") as g:
            g.write(f"code={r.code} rcept_no={r.rcept_no} rcept_dt={r.rcept_dt} report={r.report_nm}\n")
            g.write(f"sha256={hashlib.sha256(b).hexdigest()} grep={gp}\n")
            last = -10**9
            for mm in re.finditer(gp, full):
                if mm.start() - last < 1500:
                    return
                last = mm.start()
                g.write(f"--- @{mm.start()} ---\n" + full[max(0, mm.start() - 300):mm.start() + 1500] + "\n")
        print("grep 저장", gfn)
    print("저장", fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    req = pd.read_csv(a.list, dtype=str)
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, list(req.itertuples())))


if __name__ == "__main__":
    main()
