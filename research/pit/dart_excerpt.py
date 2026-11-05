"""
검토용 발췌 (커밋 가능한 크기). 정기보고서의 「사업의 내용」에서
  - 1. 사업의 개요 앞부분
  - 2. 주요 제품(및 서비스) 전체 (매출 비중 표가 주로 여기)
를 뽑아 research/pit/classification/excerpts/<code>_<rcept_no>.txt 로 저장. 원문 SHA-256 을 머리에 적는다.

  python research/pit/dart_excerpt.py --list research/pit/classification/excerpt_request.csv
  excerpt_request.csv: code,rcept_no,rcept_dt,report_nm,reason
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
            return None, None, False
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
    business_parts.sales = sa
    if ov is None and pr is None:
        return full[:OVERVIEW_CHARS + PRODUCT_CHARS], None, True
    return ov if ov is not None else full[:OVERVIEW_CHARS], pr, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    req = pd.read_csv(a.list, dtype=str)
    for r in req.itertuples():
        fn = os.path.join(OUT, f"{r.code}_{r.rcept_no}.txt")
        if os.path.exists(fn) and "재추출" not in str(r.reason):
            continue
        b = call("document.xml", {"rcept_no": r.rcept_no}, binary=True)
        if b is None:
            print("실패", r.code, r.rcept_no)
            continue
        z = zipfile.ZipFile(io.BytesIO(b))
        names = z.namelist()
        main_f = next((n for n in names if os.path.basename(n) == f"{r.rcept_no}.xml"), None) or \
            max(names, key=lambda n: z.getinfo(n).file_size)
        xml, enc = decode(z.read(main_f))
        business_parts.sales = None
        ov, pr, found = business_parts(xml)
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"code={r.code} rcept_no={r.rcept_no} rcept_dt={r.rcept_dt} report={r.report_nm}\n")
            f.write(f"sha256={hashlib.sha256(b).hexdigest()} encoding={enc} section_found={int(found)} reason={r.reason}\n")
            f.write("=== 사업의 개요 (앞부분) ===\n")
            f.write((ov or "(없음)")[:OVERVIEW_CHARS] + "\n")
            f.write("=== 주요 제품 ===\n")
            f.write((pr or "(없음)")[:PRODUCT_CHARS] + "\n")
            f.write("=== 매출 및 수주상황 ===\n")
            f.write((getattr(business_parts, "sales", None) or "(없음)")[:SALES_CHARS] + "\n")
        print("저장", fn)


if __name__ == "__main__":
    main()
