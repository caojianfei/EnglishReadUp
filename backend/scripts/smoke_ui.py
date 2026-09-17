#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M2 前端验收：在真实浏览器里跑一遍阅读器。

四项验收（对应 docs/DESIGN.md §9 的 M2 标准）：
  A. 渲染还原度 —— 正文块、标题、图注都在，没有整段丢失
  B. 图片全本地   —— 拦住一切非本机请求后仍能出图（图片已在 /media 本地化）
  C. 离线可读     —— 切断 /api 后刷新，正文从 localStorage 缓存里出来
  D. 刷新回原位   —— 滚到中间、上报、刷新，滚动位置回到原处

前置：后端 8010 与前端 5173 均已启动。
用法：python scripts/smoke_ui.py [--article-id 2] [--headed]
"""

from __future__ import annotations

import os

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

FRONT = os.environ.get("FRONT_URL", "http://127.0.0.1:5173")
API = "http://127.0.0.1:8010"

PASS, FAIL = "PASS", "FAIL"
checks: list[tuple[str, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    checks.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name} {detail}", flush=True)
    return ok


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def api_put(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def is_local(url: str) -> bool:
    """本机 = 127.0.0.1 / localhost。浏览器内部协议不算外部依赖。"""
    return url.startswith(
        (
            "http://127.0.0.1",
            "http://localhost",
            "http://[::1]",
            "data:",
            "blob:",
            "about:",
            "chrome",
        )
    )


async def main(article_id: int, headed: bool, shots: Path) -> int:
    art = api_get(f"/api/articles/{article_id}")
    if art["status"] != "done":
        print(f"文章 #{article_id} 状态是 {art['status']}，先抓完再验收。")
        return 1

    # 每次验收都从「读到开头」开始，否则上一次的进度会污染 D 项。
    # 注意不能只清 scroll_pct：还原时优先用 last_block_id，旧值会被优先命中。
    first_block = art["blocks"][0]["id"] if art["blocks"] else None
    api_put(
        f"/api/articles/{article_id}/progress",
        {"last_block_id": first_block, "scroll_pct": 0.0, "read_seconds": 0},
    )

    shots.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    failed_requests: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        ctx = await browser.new_context(viewport={"width": 1360, "height": 900})
        page = await ctx.new_page()
        page.on(
            "console",
            lambda m: console_errors.append(m.text) if m.type == "error" else None,
        )
        # 控制台只说「加载失败」，不说谁失败了 —— 把 URL 一起留下才好定位
        page.on(
            "requestfailed",
            lambda r: failed_requests.append(f"{r.url} :: {r.failure}"),
        )

        # ---------------------------------------------------------- A 首页
        print("\n[A] 首页")
        await page.goto(FRONT, wait_until="networkidle")
        await page.wait_for_selector(".er-card", timeout=15000)
        cards = await page.locator(".er-card").count()
        check(cards >= 1, "文章列表渲染", f"{cards} 张卡片")
        covers = await page.evaluate(
            """() => [...document.querySelectorAll('.er-card img')]
                     .filter(i => i.complete && i.naturalWidth > 0).length"""
        )
        check(covers >= 1, "列表封面可见", f"{covers} 张")
        await page.screenshot(path=str(shots / "01-home.png"), full_page=False)

        # ---------------------------------------------------------- B 阅读页
        print("\n[B] 阅读页渲染（本地请求白名单）")
        # 拦住一切非本机请求：图片若能显示，就证明它确实来自 /media 而不是原站。
        # handler 必须是 async —— playwright 要 await route.continue_()。
        blocked: list[str] = []

        async def only_local(route):
            if is_local(route.request.url):
                await route.continue_()
            else:
                blocked.append(route.request.url)
                await route.abort()

        await ctx.route("**/*", only_local)
        await page.goto(f"{FRONT}/a/{article_id}", wait_until="networkidle")
        await page.wait_for_selector("article .prose-en", timeout=20000)
        await page.wait_for_timeout(1200)

        h = await page.locator("article h1").inner_text()
        check(len(h) > 5, "标题渲染", h[:60])

        paras = await page.locator("article .prose-en p:not(.blk-caption)").count()
        heads = await page.locator("article .blk-heading:not(h1)").count()
        figs = await page.locator("article figure").count()
        caps = await page.locator("article figcaption").count()
        api_paras = sum(1 for b in art["blocks"] if b["type"] == "para")
        api_imgs = sum(1 for b in art["blocks"] if b["type"] == "image")
        api_heads = sum(1 for b in art["blocks"] if b["type"] == "heading")
        api_caps = sum(1 for b in art["blocks"] if b["type"] == "caption")
        print(
            f"      后端: para={api_paras} heading={api_heads} image={api_imgs} caption={api_caps}"
            f" | DOM: p={paras} h={heads} figure={figs} caption={caps}"
        )
        check(paras >= api_paras, "段落全部渲染", f"{paras} >= {api_paras}")
        check(heads >= api_heads, "小标题全部渲染", f"{heads} >= {api_heads}")
        check(figs == api_imgs, "图片块全部渲染", f"{figs}/{api_imgs}")
        check(caps >= api_caps, "图注全部渲染", f"{caps} >= {api_caps}")
        check(len(blocked) == 0, "无外部依赖请求", f"被拦 {len(blocked)} 个：{blocked[:3]}")

        # 图片真的解码出来了（naturalWidth > 0 才是真的显示了）。
        # 图片是 loading="lazy"，视口外的不会加载 —— 先滚一遍整页把懒加载全部触发。
        await page.evaluate(
            """async () => {
                const step = Math.floor(window.innerHeight * 0.8);
                for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
                    window.scrollTo(0, y);
                    await new Promise((r) => setTimeout(r, 140));
                }
                window.scrollTo(0, 0);
            }"""
        )
        await page.wait_for_timeout(1200)
        loaded, broken = await page.evaluate(
            """() => {
                const imgs = [...document.querySelectorAll('article figure img')];
                return [
                  imgs.filter(i => i.complete && i.naturalWidth > 0).length,
                  imgs.filter(i => i.complete && i.naturalWidth === 0).length,
                ];
            }"""
        )
        check(loaded >= api_imgs and broken == 0, "图片全部解码成功", f"ok={loaded} broken={broken}")
        await page.screenshot(path=str(shots / "02-reader-top.png"))

        # ---------------------------------------------------------- C 主题与排版
        print("\n[C] 主题与排版")
        await page.click("button[data-theme-swatch='sepia']")
        await page.wait_for_timeout(300)
        theme = await page.get_attribute("div[data-theme]", "data-theme")
        check(theme == "sepia", "切主题生效", f"data-theme={theme}")
        await page.screenshot(path=str(shots / "03-reader-sepia.png"))

        await page.click("button[title='深色']")
        await page.wait_for_timeout(300)
        theme = await page.get_attribute("div[data-theme]", "data-theme")
        check(theme == "dark", "深色主题生效", f"data-theme={theme}")
        await page.screenshot(path=str(shots / "04-reader-dark.png"))

        fs_before = await page.evaluate(
            "() => getComputedStyle(document.querySelector('.reader-surface')).fontSize"
        )
        await page.click("button[title='放大字号']")
        await page.wait_for_timeout(200)
        fs_after = await page.evaluate(
            "() => getComputedStyle(document.querySelector('.reader-surface')).fontSize"
        )
        check(fs_before != fs_after, "字号调节生效", f"{fs_before} → {fs_after}")

        # 刷新后主题应当保持（偏好落 localStorage）
        await page.reload(wait_until="networkidle")
        await page.wait_for_selector("article .prose-en", timeout=20000)
        theme = await page.get_attribute("div[data-theme]", "data-theme")
        check(theme == "dark", "偏好跨刷新保持", f"data-theme={theme}")

        await page.click("button[data-theme-swatch='light']")
        await page.wait_for_timeout(200)

        # ---------------------------------------------------------- D 进度恢复
        print("\n[D] 刷新回到原位置")
        total = await page.evaluate("() => document.documentElement.scrollHeight - innerHeight")
        target = int(total * 0.5)
        # 必须用真实滚轮：脚本里的 window.scrollTo 不触发 wheel 事件，
        # 前端会（正确地）判定「用户没动过」并继续做图片加载后的位置校准，
        # 那样量到的是校准结果，不是真实用户的路径。
        await page.mouse.move(680, 500)
        scrolled = 0
        while scrolled < target:
            await page.mouse.wheel(0, 500)
            scrolled += 500
            await page.wait_for_timeout(40)
        await page.wait_for_timeout(700)
        actual = await page.evaluate("() => window.scrollY")
        print(f"      滚轮滚到 {actual}（目标 {target}）")
        # 直接触发 pagehide，让前端把增量立刻交出去（否则要等 15 秒的定时上报）
        await page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
        await page.wait_for_timeout(1200)

        saved = api_get(f"/api/articles/{article_id}/progress")
        check(
            saved["last_block_id"] is not None and (saved["scroll_pct"] or 0) > 0.3,
            "中途位置已上报",
            f"block={saved['last_block_id']} pct={saved['scroll_pct']} sec={saved['read_seconds']}",
        )
        check(saved["read_seconds"] >= 1, "阅读时长已累加", f"{saved['read_seconds']}s")

        await page.reload(wait_until="networkidle")
        await page.wait_for_selector("article .prose-en", timeout=20000)
        await page.wait_for_timeout(3200)  # 等图片加载完的那几轮位置校准
        y_after = await page.evaluate("() => window.scrollY")
        drift = abs(y_after - actual) / max(actual, 1)
        check(drift < 0.12, "刷新后回到原位置", f"{actual} → {y_after}（偏差 {drift:.1%}）")
        await page.screenshot(path=str(shots / "05-reader-restored.png"))

        # ---------------------------------------------------------- E 离线可读
        print("\n[E] 离线可读（切断 /api）")
        # E 项会对 /api 故意 abort，控制台必然冒网络错误 ——
        # 所以「无 JS 报错」这一项只认 E 之前的记录。
        errors_before_offline = list(console_errors)
        failures_before_offline = list(failed_requests)
        await ctx.unroute("**/*")

        async def cut_api(route):
            if "/api/" in route.request.url:
                await route.abort()
            else:
                await route.continue_()

        await ctx.route("**/*", cut_api)
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector("article .prose-en", timeout=20000)
        await page.wait_for_timeout(1200)
        paras_off = await page.locator("article .prose-en p").count()
        offline_chip = await page.locator("text=离线阅读").count()
        check(paras_off >= api_paras, "断网后正文仍在（走缓存）", f"{paras_off} 段")
        check(offline_chip > 0, "顶栏提示离线阅读")
        await page.screenshot(path=str(shots / "06-reader-offline.png"))
        await ctx.unroute("**/*")

        # ---------------------------------------------------------- F 控制台
        print("\n[F] 控制台与请求")
        # 主动拦截造成的失败不算问题，只看「没人拦却失败」的请求
        external_failed = [
            f for f in failures_before_offline if not is_local(f.split(" :: ")[0])
        ]
        check(len(external_failed) == 0, "无非本机请求失败", str(external_failed[:3]))

        # 网络资源失败（B 项的拦截）不算 JS 错误，真正的 JS 运行时异常才要拦
        js_errors = [
            e
            for e in errors_before_offline
            if "Failed to load resource" not in e and "favicon" not in e.lower()
        ]
        check(len(js_errors) == 0, "无 JS 运行时错误", str(js_errors[:2]))
        if failures_before_offline:
            print(f"       被拦下的请求：{failures_before_offline[:6]}")

        await browser.close()

    ok = sum(1 for s, _, _ in checks if s == PASS)
    print(f"\n{'=' * 60}\n结果：{ok}/{len(checks)} 通过")
    for s, name, detail in checks:
        if s == FAIL:
            print(f"  [FAIL] {name} {detail}")
    print(f"截图：{shots}")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=2)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    out = Path(__file__).resolve().parents[1] / "data" / "m2_shots"
    raise SystemExit(asyncio.run(main(args.article_id, args.headed, out)))
