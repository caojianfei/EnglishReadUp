# -*- coding: utf-8 -*-
"""诊断 6：用 Playwright 渲染页面，拿到图片在正文中的真实位置（基准真值）。

用法：probe_render.py <url> [waitSelector]
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

url = sys.argv[1]
wait_sel = sys.argv[2] if len(sys.argv) > 2 else None

FIND_AND_DUMP = r"""
() => {
  const score = (el) => el.querySelectorAll(':scope > p, :scope > figure, :scope > div').length;
  let best = null, bestN = 0;
  for (const el of document.querySelectorAll('article, main, section, div')) {
    const inner = el.querySelectorAll('p').length;
    if (inner < 5) continue;
    const direct = score(el);
    const depth = (function d(n){ let r=0; while(n && n!==document.body){n=n.parentElement;r++;} return r; })(el);
    const val = inner - depth * 0.5;
    if (val > bestN) { bestN = val; best = el; }
  }
  if (!best) return { error: 'no container' };
  const children = [...best.children].map(el => {
    const img = el.tagName === 'IMG' ? el : el.querySelector('img');
    return {
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 48),
      img: img ? (img.currentSrc || img.src || '') : '',
      imgAlt: img ? (img.alt || '') : '',
      text: (el.textContent || '').trim().slice(0, 46)
    };
  });
  return {
    container: best.tagName.toLowerCase() + '.' + (best.className || '').toString().slice(0, 48),
    childCount: children.length,
    children
  };
}
"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        viewport={"width": 1400, "height": 1000},
    )
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if wait_sel:
        try:
            page.wait_for_selector(wait_sel, timeout=20000)
        except Exception as e:
            print("等待选择器超时:", e)
    page.wait_for_timeout(4000)
    # 滚动到底，触发懒加载
    for _ in range(8):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(600)
    page.wait_for_timeout(1500)

    total_img = page.eval_on_selector_all("img", "els => els.length")
    in_article = page.eval_on_selector_all(
        "article img, main img, figure img", "els => els.map(e => e.currentSrc || e.src)")
    print("=" * 78)
    print("页面 <img> 总数:", total_img)
    print("article/main/figure 内 <img>:", len(in_article))
    for u in in_article[:25]:
        print("   ", u[:110])
    print("=" * 78)

    res = page.evaluate(FIND_AND_DUMP)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:9000])
    browser.close()
