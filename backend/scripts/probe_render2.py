# -*- coding: utf-8 -*-
"""诊断 7：以封面图为锚，dump 正文容器的完整子节点序列，看图片落在第几段之后。"""
import json
import sys

from playwright.sync_api import sync_playwright

url = sys.argv[1]

DUMP = r"""
() => {
  const cover = document.querySelector('figure[class*="coverImgBox"], figure[class*="cover"]');
  let box = cover ? cover.parentElement : null;
  if (!box) {
    const cands = [...document.querySelectorAll('section, article, div')]
      .map(el => ({el, n: el.querySelectorAll(':scope > p').length}))
      .sort((a, b) => b.n - a.n);
    box = cands.length ? cands[0].el : null;
  }
  if (!box) return {error: 'no box'};
  const kids = [...box.children];
  const out = kids.map((el, i) => {
    const imgs = [...el.querySelectorAll('img')].map(e => e.currentSrc || e.src || '');
    const cap = el.querySelector('figcaption, [class*="caption"], [class*="desc"]');
    return {
      i,
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 40),
      imgs,
      cap: cap ? (cap.textContent || '').trim().slice(0, 70) : '',
      text: (el.textContent || '').trim().slice(0, 50)
    };
  });
  return {
    boxTag: box.tagName.toLowerCase() + '.' + (box.className || '').toString().slice(0, 46),
    total: kids.length,
    out
  };
}
"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        viewport={"width": 1400, "height": 1000})
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(4000)
    for _ in range(10):
        pg.mouse.wheel(0, 1600)
        pg.wait_for_timeout(500)
    pg.wait_for_timeout(1500)

    res = pg.evaluate(DUMP)
    b.close()

if "error" in res:
    print(json.dumps(res, ensure_ascii=False))
    sys.exit(0)

print("容器:", res["boxTag"], " 子节点数:", res["total"])
print("-" * 78)
for it in res["out"]:
    if it["imgs"]:
        print(f"[{it['i']:>3}] <{it['tag']}> {it['cls']}")
        for u in it["imgs"]:
            print(f"         IMG {u[:100]}")
        if it["cap"]:
            print(f"         CAP {it['cap']}")
    else:
        print(f"[{it['i']:>3}] <{it['tag']}> {it['text'][:60]!r}   cls={it['cls']!r}")
