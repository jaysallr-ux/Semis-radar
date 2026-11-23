import os, re, json, sys, datetime, asyncio, requests

OUT = "output/kospi_pbr.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BASE = "https://data.krx.co.kr"
MENU = BASE + "/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201"


class Fail(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def kst_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def num(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def zone(pbr):
    if pbr is None:
        return "알수없음"
    if pbr <= 0.8:
        return "2차"
    if pbr <= 0.9:
        return "1차"
    return "대기"


async def login_cookies(krx_id, krx_pw):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    dialogs = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent=UA, locale="ko-KR", viewport={"width": 1920, "height": 1080})
        page = await ctx.new_page()

        async def on_dialog(d):
            dialogs.append(d.message)
            await d.dismiss()
        page.on("dialog", on_dialog)

        try:
            resp = await page.goto(BASE + "/contents/MDC/COMS/client/MDCCOMS001.cmd", wait_until="networkidle", timeout=40000)
        except PWTimeout:
            raise Fail("KRX_CONNECT", "KRX 로그인 페이지 접속 시간초과 (서버 장애 또는 해외 IP 차단 의심)")
        except Exception as e:
            raise Fail("KRX_CONNECT", "KRX 로그인 페이지 접속 실패: " + str(e)[:150])
        if resp is not None and resp.status >= 400:
            raise Fail("KRX_BLOCKED", "KRX 로그인 페이지 HTTP " + str(resp.status) + " (접속 차단 의심)")
        await asyncio.sleep(2)

        try:
            iframe = await page.query_selector("iframe")
            if iframe is None:
                raise Fail("LOGIN_PAGE_CHANGED", "로그인 iframe 없음 (KRX 로그인 화면 구조 변경)")
            frame = await iframe.content_frame()
            await (await frame.wait_for_selector("#mbrId", timeout=15000)).fill(krx_id)
            await (await frame.wait_for_selector('input[name="pw"]', timeout=15000)).fill(krx_pw)
            await (await frame.wait_for_selector("a.jsLoginBtn", timeout=15000)).click()
        except Fail:
            raise
        except PWTimeout:
            raise Fail("LOGIN_PAGE_CHANGED", "아이디/비번 입력칸 또는 로그인 버튼을 못 찾음 (KRX 로그인 화면 구조 변경)")
        await asyncio.sleep(4)

        try:
            await page.goto(MENU, wait_until="networkidle", timeout=40000)
        except PWTimeout:
            raise Fail("KRX_CONNECT", "로그인 후 데이터 화면 접속 시간초과")
        if "MDCCOMS001" in page.url:
            msg = (" / KRX 메시지: " + " | ".join(dialogs)) if dialogs else ""
            raise Fail("LOGIN_FAILED", "로그인 거부 (아이디·비번 오류, 비번 변경 요구, 계정 잠김 중 하나)" + msg)
        cookies = await ctx.cookies([BASE])
        await b.close()
        if not any(c["name"] == "mdc.client_session" for c in cookies):
            raise Fail("LOGIN_FAILED", "로그인은 넘어갔는데 세션 쿠키(mdc.client_session)가 안 나옴")
        return cookies


def fetch(s, day):
    try:
        r = s.post(BASE + "/comm/bldAttendant/getJsonData.cmd", data={
            "bld": "dbms/MDC/STAT/standard/MDCSTAT00701",
            "locale": "ko_KR",
            "searchType": "A",
            "idxIndMidclssCd": "02",
            "trdDd": day,
            "csvxls_isNo": "false",
        }, timeout=20)
    except requests.Timeout:
        raise Fail("KRX_CONNECT", "PBR 데이터 요청 시간초과")
    except requests.RequestException as e:
        raise Fail("KRX_CONNECT", "PBR 데이터 요청 실패: " + str(e)[:150])
    if r.status_code in (401, 403, 429):
        raise Fail("KRX_BLOCKED", "PBR 데이터 요청 HTTP " + str(r.status_code) + " (차단 또는 요청 제한)")
    if r.status_code >= 400:
        raise Fail("KRX_HTTP", "PBR 데이터 요청 HTTP " + str(r.status_code))
    if "text/html" in r.headers.get("Content-Type", "") or not r.text.strip():
        raise Fail("SESSION_INVALID", "JSON 대신 HTML/빈 응답 (세션이 안 먹힘)")
    try:
        j = r.json()
    except Exception:
        raise Fail("RESPONSE_CHANGED", "응답이 JSON이 아님: " + r.text[:120])
    if j.get("RESULT") == "LOGOUT":
        raise Fail("SESSION_INVALID", "KRX가 LOGOUT 반환 (세션 만료 또는 로그인 미인정)")
    return next((v for v in j.values() if isinstance(v, list)), None)


def get_official():
    krx_id = os.environ.get("KRX_ID", "").strip()
    krx_pw = os.environ.get("KRX_PW", "").strip()
    if not krx_id or not krx_pw:
        raise Fail("SECRETS_MISSING", "깃허브 Secrets에 KRX_ID 또는 KRX_PW가 없음")
    try:
        cookies = asyncio.run(login_cookies(krx_id, krx_pw))
    except Fail:
        raise
    except Exception as e:
        raise Fail("UNKNOWN", "로그인 단계 예외: " + type(e).__name__ + " " + str(e)[:150])
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": MENU, "Origin": BASE, "X-Requested-With": "XMLHttpRequest"})
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c["domain"])
    today = kst_now()
    sample_keys = None
    for i in range(8):
        d = (today - datetime.timedelta(days=i)).strftime("%Y%m%d")
        rows = fetch(s, d)
        if rows is None:
            raise Fail("RESPONSE_CHANGED", "응답에 데이터 목록이 없음 (KRX 응답 구조 변경)")
        if i == 0:
            print("RAW sample:", json.dumps(rows[:2], ensure_ascii=False))
        if not rows:
            continue
        sample_keys = list(rows[0].keys())
        row = next((r for r in rows if str(r.get("IDX_NM", "")).strip() == "코스피"), None)
        if row is None:
            raise Fail("RESPONSE_CHANGED", "코스피 행을 못 찾음. 응답 키: " + ",".join(sample_keys))
        pbr = num(row.get("WT_STKPRC_NETASST_RTO"))
        if pbr is None:
            if "WT_STKPRC_NETASST_RTO" not in row:
                raise Fail("RESPONSE_CHANGED", "PBR 컬럼 이름이 바뀜. 응답 키: " + ",".join(sample_keys))
            continue
        return {"date": d, "close": num(row.get("CLSPRC_IDX")), "pbr": pbr, "per": num(row.get("WT_PER")), "div_yield": num(row.get("DIV_YD"))}
    raise Fail("NO_DATA", "최근 8일 동안 코스피 PBR 데이터 없음 (장기 휴장 또는 KRX 데이터 미갱신)")


def naver_close():
    try:
        x = requests.get("https://fchart.stock.naver.com/sise.nhn?symbol=KOSPI&timeframe=day&count=2&requestType=0", headers={"User-Agent": UA}, timeout=20).text
        d, o, h, l, c, v = re.findall(r'data="([^"]+)"', x)[-1].split("|")
        return d, float(c)
    except Exception as e:
        raise Fail("NAVER_FAIL", "네이버 코스피 종가 조회 실패: " + type(e).__name__)


def load_prev():
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return {}


def main():
    prev = load_prev()
    os.makedirs("output", exist_ok=True)
    now = kst_now().strftime("%Y-%m-%d %H:%M")
    try:
        o = get_official()
        z = zone(o["pbr"])
        out = {
            "status": "ok",
            "source": "KRX 공식",
            "date": o["date"],
            "close": o["close"],
            "pbr": o["pbr"],
            "per": o["per"],
            "div_yield": o["div_yield"],
            "zone": z,
            "prev_zone": prev.get("zone"),
            "zone_changed": prev.get("zone") not in (None, z),
            "fail_count": 0,
            "last_ok_kst": now,
            "updated_kst": now,
        }
    except Fail as f:
        code, reason = f.code, f.reason
        out = {k: prev.get(k) for k in ("date", "close", "pbr", "per", "div_yield", "zone", "last_ok_kst") if k in prev}
        out.update({
            "status": "stale",
            "source": "추정 (공식 조회 실패)",
            "zone_changed": False,
            "fail_code": code,
            "fail_reason": reason,
            "fail_count": int(prev.get("fail_count") or 0) + 1,
            "updated_kst": now,
        })
        if not out.get("pbr") or not out.get("close"):
            out["est_status"] = "추정불가: 기준이 될 공식 PBR이 아직 한 번도 수집 안 됨"
        else:
            try:
                nd, nc = naver_close()
                est = round(out["pbr"] * nc / out["close"], 3)
                ze = zone(est)
                base_age = (datetime.datetime.strptime(nd, "%Y%m%d") - datetime.datetime.strptime(out["date"], "%Y%m%d")).days
                prev_ze = prev.get("zone_est") or prev.get("zone")
                out.update({
                    "est_status": "ok",
                    "pbr_est": est,
                    "zone_est": ze,
                    "zone_est_changed": prev_ze is not None and ze != prev_ze,
                    "est_date": nd,
                    "est_close": nc,
                    "est_basis": "공식 PBR " + str(out["pbr"]) + " (" + out["date"] + ", 코스피 " + str(out["close"]) + ") x (" + str(nc) + " / " + str(out["close"]) + ")",
                    "base_age_days": base_age,
                    "base_warning": "기준 공식값이 " + str(base_age) + "일 지남. 그 사이 실적 반영으로 실제와 차이 클 수 있음" if base_age > 30 else "",
                })
            except Fail as f2:
                out["est_status"] = "추정불가: " + f2.reason
        print("FAIL:", code, reason, file=sys.stderr)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
