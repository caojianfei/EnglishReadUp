# -*- coding: utf-8 -*-
"""诊断 3：正文图片到底在哪、为什么没进 blocks。

用法：probe_images.py <raw.html> [正文容器选择器]
"""
import json
import re
import sys
from pathlib import Path

import trafilatura
from selectolax.parser import HTMLParser

raw_path = Path(sys.argv[1])
selector = sys.argv[2] if len(sys.argv) > 2 else None
raw = raw_path.read_text(encoding="utf-8")

print("=" * 78)
print("文件:", raw_path.name)
print("=" * 78)

tree = HTMLParser(raw)
imgs = tree.css("img")
print(f"\n[1] 原始 DOM 中 <img> 总数: {len(imgs)}")
for i, im in enumerate(imgs):
    a = im.attributes or {}
    src = a.get("src") or a.get("data-src") or a.get("data-original") or ""
    if not src:
        continue
    chain = []
    cur, d = im.parent, 0
    while cur is not None and d < 5:
        att = cur.attributes or {}
        ident = att.get("id") or ""
        cls = att.get("class") or ""
        if ident or cls:
            chain.append(f"{cur.tag}#{ident}.{cls[:30]}")
        cur = cur.parent
        d += 1
    print(f"  [{i:>2}] {src[:100]}")
    print(f"       alt={a.get('alt','')[:50]!r}  srcset={'有' if a.get('srcset') else '无'}")
    print(f"       祖先: {' < '.join(chain) if chain else '(无标识)'}")

print(f"\n[2] <picture>/<source>/<figure> 计数: "
      f"picture={len(tree.css('picture'))} source={len(tree.css('source'))} "
      f"figure={len(tree.css('figure'))} figcaption={len(tree.css('figcaption'))}")

bg = re.findall(r"background-image\s*:\s*url\((['\"]?)([^)'\"]+)\1\)", raw, re.I)
print(f"[3] CSS background-image 引用: {len(bg)} 个")
for _, u in bg[:8]:
    print(f"      {u[:110]}")

m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
print(f"[4] Next.js __NEXT_DATA__: {'存在' if m else '不存在'}")
if m:
    try:
        blob = json.loads(m.group(1))
        found = []

        def dig(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str) and re.search(r"\.(jpe?g|png|webp|gif)", v, re.I):
                        found.append((k, v))
                    else:
                        dig(v)
            elif isinstance(o, list):
                for v in o:
                    dig(v)

        dig(blob)
        print(f"      内嵌图片 URL: {len(found)} 个")
        for k, v in found[:20]:
            print(f"      {k:<16} {v[:100]}")
    except Exception as e:
        print("      解析失败:", e)

if selector:
    node = tree.css_first(selector)
    print(f"\n[5] 容器 {selector}: {'命中' if node else '未命中'}")
    if node is not None:
        h = node.html or ""
        print(f"      容器内 <img>: {len(node.css('img'))} 个")
        print(f"      容器内 <figure>: {len(node.css('figure'))} 个")
        print(f"      容器 HTML 长度: {len(h)}")
        print("      容器 HTML 前 800 字符:")
        print("      " + h[:800].replace("\n", "\n      "))

print("\n[6] trafilatura 各配置下输出的 <img> 数")
for label, kw in (
    ("默认(不传 include_images)", {}),
    ("include_images=True", dict(include_images=True)),
    ("include_images=True + favor_recall", dict(include_images=True, favor_recall=True, favor_precision=False)),
):
    src = raw
    if selector:
        n = tree.css_first(selector)
        if n is not None:
            src = n.html or raw
    try:
        out = trafilatura.extract(src, output_format="html", include_formatting=True,
                                  include_tables=True, include_comments=False, **kw)
    except Exception as e:
        print(f"      {label:<34} EXC {type(e).__name__}: {e}")
        continue
    n_img = len(re.findall(r"<img", out or "", re.I))
    print(f"      {label:<34} len={len(out or ''):>6}  <img>={n_img}")
