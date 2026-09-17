# -*- coding: utf-8 -*-
"""诊断 8：算出每张正文图「最近的前一个非空段落」—— 这就是插入锚点。"""
import json
import sys

from playwright.sync_api import sync_playwright

url = sys.argv[1]

DUMP = r"""
() => {
  const cover = document.querySelector('figure[class*="coverImgBox"]');
  const box = cover ? cover.parentElement : document.body;
  const ps = [...box.querySelectorAll('p')];
  const info = ps.map((p, i) => {
    const t = (p.textContent || '').trim();
    return {i, empty: t.length === 0, text: t.slice(0, 60)};
  });
  const nonEmptyIdx = info.filter(x => !x.empty).map(x => x.i);

  const imgs = [...box.querySelectorAll('img')];
  const res = imgs.map(img => {
    let prev = -1;
    for (const x of nonEmptyIdx) {
      const pEl = ps[x];
      if (pEl.compareDocumentPosition(img) & Node.DOCUMENT_POSITION_FOLLOWING) prev = x;
      else break;
    }
    return {
      src: img.currentSrc || img.src,
      prevParaAbs: prev,
      prevParaOrdinal: prev === -1 ? -1 : nonEmptyIdx.indexOf(prev),
      prevText: prev === -1 ? '' : (ps[prev].textContent || '').trim().slice(0, 60)
    };
  });
  return {
    boxTag: box.tagName.toLowerCase() + '.' + (box.className || '').toString().slice(0, 40),
    pTotal: info.length,
    pEmpty: info.filter(x => x.empty).length,
    nonEmptyOrdinals: nonEmptyIdx,
    images: res
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

print("容器:", res["boxTag"])
print(f"<p> 总数: {res['pTotal']}  空 <p>: {res['pEmpty']}  非空 <p>: {len(res['nonEmptyOrdinals'])}")
print("-" * 78)
for k, im in enumerate(res["images"]):
    name = im["src"].rsplit("/", 1)[-1]
    print(f"[{k:>2}] {name:<12} 落在第 {im['prevParaOrdinal']:>3} 个非空段之后")
    print(f"      前段: {im['prevText']!r}")
