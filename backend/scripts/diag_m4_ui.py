"""一次性诊断：深链的合成痕迹为什么没渲染 + 移除时的 ERR_ABORTED 是谁发的。"""

from __future__ import annotations

import os

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

BACKEND = "http://127.0.0.1:8010"
FRONTEND = os.environ.get("FRONT_URL", "http://127.0.0.1:5173")
AID = 2

MARKS = """
() => {
  const art = document.querySelector('article');
  return {
    hasArticle: !!art,
    marks: [...document.querySelectorAll('mark')].map((m) => ({
      hlId: m.dataset.hlId ?? null,
      inArticle: !!m.closest('article'),
      block: m.closest('[data-block-id]')?.dataset.blockId ?? null,
      text: m.textContent.slice(0, 24),
      cls: m.className,
    })),
  };
}
"""


async def main() -> None:
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        hl = c.get(f"/api/articles/{AID}/highlights").json()["items"]
        art = c.get(f"/api/articles/{AID}").json()
        blocks = {b["id"]: (b.get("text") or "") for b in art["blocks"]}
    print("库里痕迹：", [(h["id"], h["block_id"], h["start_offset"], h["end_offset"]) for h in hl])

    # 随便挑一条，走深链
    probe = hl[0]
    target = f"{FRONTEND}/a/{AID}?block={probe['block_id']}&start={probe['start_offset']}&end={probe['end_offset']}"
    print("深链：", target)
    print("目标块文本长度：", len(blocks.get(probe["block_id"], "")))

    events: list[str] = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await (await b.new_context(viewport={"width": 1440, "height": 920})).new_page()
        page.on("request", lambda r: events.append(f"REQ  {r.method} {r.url}")
                if "/api/vocab" in r.url else None)
        page.on("response", lambda r: events.append(f"RESP {r.status} {r.url}")
                if "/api/vocab" in r.url else None)
        page.on("requestfailed", lambda r: events.append(
            f"FAIL {r.method} {r.url} {r.failure}"))
        page.on("console", lambda m: events.append(f"CONSOLE[{m.type}] {m.text}")
                if m.type in ("error", "warning") else None)

        await page.goto(target, wait_until="networkidle")
        await page.wait_for_selector("article .prose-en p", timeout=25000)
        await page.wait_for_timeout(1500)
        info = await page.evaluate(MARKS)
        print("article 存在：", info["hasArticle"])
        print("页面上所有 mark：")
        for m in info["marks"]:
            print("   ", m)
        els = await page.evaluate(
            "() => [...document.querySelectorAll('[data-block-id]')].map(e => Number(e.dataset.blockId))"
        )
        print("已渲染的块 id：", sorted(els))
        print("目标块是否已渲染：", probe["block_id"] in els)
        url = page.url
        print("URL：", url)

        # ---- 移除时的网络行为 ----
        events.clear()
        with httpx.Client(base_url=BACKEND, timeout=180) as c:
            r = c.get("/api/dict/obstreperous", params={
                "article_id": AID,
                "sentence": "An obstreperous crowd gathered during the diagnosis run.",
            })
            enr = r.json().get("enrolled") or {}
            vid = enr.get("vocab_id")
        print("造词结果：", enr)
        await page.goto(f"{FRONTEND}/vocab", wait_until="networkidle")
        await page.wait_for_timeout(1200)
        sel = f'[data-testid="vocab-row"][data-lemma="obstreperous"]'
        if await page.query_selector(sel) is None:
            await page.fill('[data-testid="vocab-search"]', "obstreperous")
            await page.wait_for_timeout(1200)
        print("找到卡片：", await page.query_selector(sel) is not None)
        await page.click(f"{sel} >> text=移除")
        await page.wait_for_timeout(1500)
        print("移除后的网络事件：")
        for e in events:
            print("   ", e)
        if vid:
            with httpx.Client(base_url=BACKEND, timeout=30) as c:
                print("后端还剩这条吗：", c.get(f"/api/vocab/{vid}").status_code)
                c.delete(f"/api/vocab/{vid}")
        await b.close()


asyncio.run(main())
