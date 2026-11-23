import asyncio, re
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await (await b.new_context(locale="ko-KR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", viewport={"width":1920,"height":1080})).new_page()
        await page.goto("https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd", wait_until="domcontentloaded")
        await asyncio.sleep(10)
        print("URL", page.url)
        top = await page.content()
        print("TOPLEN", len(top), re.sub(r"\s+"," ",top)[:1500])
        for f in page.frames:
            print("FRAME", f.url)
        fr = page.frames[-1]
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
