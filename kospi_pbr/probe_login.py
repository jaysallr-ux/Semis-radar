import asyncio, re
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await (await b.new_context(locale="ko-KR")).new_page()
        await page.goto("https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd", wait_until="domcontentloaded")
        await page.wait_for_selector("iframe", timeout=20000)
        await asyncio.sleep(3)
        for f in page.frames:
            print("FRAME", f.url)
        fr = (await (await page.query_selector("iframe")).content_frame())
        html = await fr.content()
        for m in re.findall(r"<form[\s\S]*?</form>", html)[:3]:
            print("FORM", re.sub(r"\s+", " ", m)[:3000])
        for m in re.findall(r"<script[^>]*src=\"[^\"]+\"", html):
            print("SCRIPT", m)
        i = html.find("jsLoginBtn")
        print("BTN", re.sub(r"\s+", " ", html[max(0,i-400):i+300]))
        for m in re.findall(r"<script(?![^>]*src)[^>]*>([\s\S]*?)</script>", html):
            if "login" in m.lower() or "pw" in m:
                print("INLINE", re.sub(r"\s+", " ", m)[:4000])
        await b.close()
asyncio.run(main())
