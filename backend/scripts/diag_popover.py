"""聚焦诊断：为什么选段后点动作按钮，右侧 AI 面板不出现？

只做一件事 —— 把「选区 → 浮层 → 点按钮 → 面板」这条链上每一步的
DOM 状态打印出来，并在点击前后抓取控制台报错。

跑法（后端 8010 + 前端 5173 都要在跑）：
    python -u scripts/diag_popover.py --article-id 2 --headed
"""

from __future__ import annotations

import os

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

BACKEND = "http://127.0.0.1:8010"
FRONTEND = os.environ.get("FRONT_URL", "http://127.0.0.1:5173")
POP = '[data-testid="selection-popover"]'
PANEL = '[data-testid="ai-panel"]'

SELECT_JS = """
([blockId, wanted]) => {
  const el = document.querySelector(`[data-block-id="${blockId}"]`);
  if (!el) return { ok: false, why: 'block not found' };
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let full = '';
  let n;
  while ((n = walker.nextNode())) {
    nodes.push({ node: n, start: full.length });
    full += n.textContent || '';
  }
  const at = full.indexOf(wanted);
  if (at < 0) return { ok: false, why: 'text not found', len: full.length };
  const locate = (off) => {
    for (const it of nodes) {
      const len = (it.node.textContent || '').length;
      if (off <= it.start + len) return { node: it.node, offset: off - it.start };
    }
    const last = nodes[nodes.length - 1];
    return { node: last.node, offset: (last.node.textContent || '').length };
  };
  const s = locate(at);
  const e = locate(at + wanted.length);
  const r = document.createRange();
  r.setStart(s.node, s.offset);
  r.setEnd(e.node, e.offset);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  // 上下都收敛到视口内，再派发 mouseup（与 smoke_ui_m3.py 的 SELECT_JS 一致）
  const before = r.getBoundingClientRect();
  if (before.top < 140 || before.bottom > window.innerHeight - 140) {
    window.scrollBy(0, before.top - Math.round(window.innerHeight * 0.35));
  }
  el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  const after = r.getBoundingClientRect();
  return { ok: true, text: r.toString(), y: after.top };
}
"""

SNAP_JS = """
() => {
  const pop = document.querySelector('[data-testid="selection-popover"]');
  const panel = document.querySelector('[data-testid="ai-panel"]');
  const p = pop ? pop.getBoundingClientRect() : null;
  return {
    popExists: !!pop,
    popKind: pop ? pop.getAttribute('data-kind') : null,
    popRect: p ? {x: Math.round(p.x), y: Math.round(p.y), w: Math.round(p.width), h: Math.round(p.height)} : null,
    popInView: p ? (p.top >= 0 && p.bottom <= innerHeight + 1) : null,
    panelExists: !!panel,
    scrollY: Math.round(window.scrollY),
    innerH: innerHeight,
    popButtons: pop ? [...pop.querySelectorAll('button')].map(b => b.textContent.trim()) : [],
  };
}
"""


def pick_article(want: int | None) -> dict:
    r = httpx.get(f"{BACKEND}/api/articles?limit=20", timeout=20)
    r.raise_for_status()
    items = [a for a in r.json()["items"] if a["status"] == "done"]
    if want is not None:
        hit = [a for a in items if a["id"] == want]
        if not hit:
            raise SystemExit(f"文章 {want} 不可用")
        return hit[0]
    items.sort(key=lambda a: a["word_count"], reverse=True)
    return items[0]


def blocks_of(article_id: int) -> list[dict]:
    r = httpx.get(f"{BACKEND}/api/articles/{article_id}", timeout=30)
    r.raise_for_status()
    return r.json()["blocks"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    art = pick_article(args.article_id)
    aid = art["id"]
    blocks = blocks_of(aid)
    print(f"文章 #{aid} {art['title'][:52]}")
    print(f"块数 {len(blocks)}")

    # 找一块够长、含完整句子的段落
    para = None
    for b in blocks:
        if b["type"] != "para":
            continue
        m = re.search(r"[A-Z][^.!?]{40,190}[.!?]", b["text"] or "")
        if m:
            para = (b, m.group(0))
            break
    if para is None:
        raise SystemExit("没有可用段落")
    blk, sent = para
    print(f"测试段 block={blk['id']}  句长={len(sent)}")
    print(f"  句子: {sent[:100]}...")

    errors: list[str] = []
    console: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(f"{FRONTEND}/a/{aid}", wait_until="networkidle")
        await page.wait_for_selector("article", timeout=20000)
        await page.wait_for_timeout(1200)

        print("\n--- 1. 建立选区 ---")
        r = await page.evaluate(SELECT_JS, [blk["id"], sent])
        print("   ", r)
        for t in (300, 700, 1200, 2000, 3000):
            await page.wait_for_timeout(t if t == 300 else t)
            s = await page.evaluate(SNAP_JS)
            print(f"    +{t}ms  popExists={s['popExists']} popKind={s['popKind']} "
                  f"popInView={s['popInView']} popRect={s['popRect']} scrollY={s['scrollY']}")
            if s["popExists"] and s["popButtons"]:
                break
        s = await page.evaluate(SNAP_JS)
        if not s["popExists"]:
            print("    浮层元素根本不存在 —— 停在这里")
            print("    pageerror:", errors or "无")
            for c in console[-15:]:
                print("     ", c)
            await page.screenshot(path=str(Path(__file__).resolve().parents[1] / "data" / "diag-no-popover.png"))
            await browser.close()
            return
        print("    buttons:", s["popButtons"])
        print("    popInView:", s["popInView"], " popRect:", s["popRect"], " innerH:", s["innerH"])

        print("\n--- 3. 点击前：让 playwright 自己做 scrollIntoViewIfNeeded ---")
        loc = page.locator('[data-testid="parse-action"]').first
        try:
            await loc.scroll_into_view_if_needed(timeout=4000)
            print("    scrollIntoViewIfNeeded 成功")
        except Exception as e:  # noqa: BLE001
            print(f"    scrollIntoViewIfNeeded 失败: {type(e).__name__}: {e}")
        s = await page.evaluate(SNAP_JS)
        print("    之后 popExists:", s["popExists"], " scrollY:", s["scrollY"])
        if not s["popExists"]:
            print("    >>> 浮层被这一下滚动关掉了（scroll 关闭误伤）")
            await page.screenshot(path=str(Path(__file__).resolve().parents[1] / "data" / "diag-after-scroll.png"))
            await browser.close()
            print("\npageerror:", errors)
            return

        print("\n--- 4. 真点按钮 ---")
        clicked = False
        for sel in ('[data-testid="parse-action"]', f"{POP} >> text=解析"):
            try:
                await page.click(sel, timeout=4000)
                print(f"    点击「{sel}」成功")
                clicked = True
                break
            except Exception as e:  # noqa: BLE001
                print(f"    点「{sel}」失败: {type(e).__name__}: {str(e)[:120]}")
        if not clicked:
            await page.screenshot(path=str(Path(__file__).resolve().parents[1] / "data" / "diag-click-fail.png"))

        print("\n--- 5. 点击后状态 ---")
        await page.wait_for_timeout(1500)
        s = await page.evaluate(SNAP_JS)
        print("    popExists:", s["popExists"], " panelExists:", s["panelExists"])
        try:
            if await page.wait_for_selector(PANEL, timeout=6000):
                print("    >>> 面板出来了")
            else:
                print("    >>> 面板 6s 内没出现")
        except Exception:  # noqa: BLE001
            print("    >>> 面板没出现")

        await page.screenshot(path=str(Path(__file__).resolve().parents[1] / "data" / "diag-final.png"), full_page=False)
        print("\npageerror:", errors or "无")
        print("console（后 20 条）:")
        for c in console[-20:]:
            print("   ", c)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
