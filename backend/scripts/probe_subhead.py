#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：渲染后正文里的小标题到底是什么元素。

背景：Sixth Tone 的正文小标题（如 `Life on the move`）在渲染后的 DOM 里
不是 `<h*>`，当前按 class 特征识别没命中，落成了普通段落。
这个脚本把候选元素的 tag / class / 计算样式打出来，据此决定识别规则。

用法：python scripts/probe_subhead.py <url> <小标题文本>
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services.fetcher import PICK_CONTAINER_JS, _noop  # noqa: E402

FIND_JS = r"""
(target) => {
  const out = [];
  const walk = (el) => {
    const own = [...el.childNodes]
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').trim();
    if (own === target) {
      const cs = getComputedStyle(el);
      out.push({
        tag: el.tagName, cls: String(el.className || ''),
        fontSize: cs.fontSize, fontWeight: cs.fontWeight, marginTop: cs.marginTop,
        parentTag: el.parentElement ? el.parentElement.tagName : '',
        parentCls: el.parentElement ? String(el.parentElement.className || '') : '',
      });
    }
    for (const c of el.children) walk(c);
  };
  walk(document.body);

  // 正文容器的直属子节点概览，用于看小标题在结构里的位置
  const cover = document.querySelector('figure[class*="coverImgBox"], figure[class*="cover"]');
  let box = cover ? cover.parentElement : null;
  if (!box) {
    const cands = [...document.querySelectorAll('article, main, section, div')]
      .map(el => ({el, n: el.querySelectorAll(':scope > p').length}))
      .filter(x => x.n >= 3)
      .sort((a, b) => b.n - a.n);
    box = cands.length ? cands[0].el : document.body;
  }
  const kids = box ? [...box.children].slice(0, 14).map(c => ({
    tag: c.tagName, cls: String(c.className || '').slice(0, 60),
    fs: getComputedStyle(c).fontSize, fw: getComputedStyle(c).fontWeight,
    text: (c.textContent || '').trim().slice(0, 40),
  })) : [];
  return {matches: out, container: box ? (box.tagName + '.' + String(box.className || '')) : '', kids};
}
"""


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    url, target = sys.argv[1], sys.argv[2]

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=settings.fetch_ua, viewport={"width": 1400, "height": 1000})
        page.goto(url, wait_until="domcontentloaded", timeout=settings.render_timeout_s * 1000)
        page.wait_for_timeout(settings.render_wait_ms)
        for _ in range(10):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(400)
        page.wait_for_timeout(1200)

        data = page.evaluate(FIND_JS, target)
        box_html = page.evaluate(PICK_CONTAINER_JS)
        browser.close()

    print("容器:", data["container"], f"({len(box_html)} 字符)")
    print("\n命中元素:")
    for m in data["matches"]:
        print(f"  <{m['tag']} class={m['cls']!r}> parent=<{m['parentTag']} class={m['parentCls']!r}>"
              f"  font={m['fontSize']}/{m['fontWeight']}  marginTop={m['marginTop']}")
    if not data["matches"]:
        print("  (无) —— 该文本可能跨多个文本节点")

    print("\n容器前 14 个直属子节点:")
    for k in data["kids"]:
        print(f"  <{k['tag']} class={k['cls']!r}> {k['fs']}/{k['fw']} {k['text']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
