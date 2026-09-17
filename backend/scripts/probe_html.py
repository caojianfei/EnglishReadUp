# -*- coding: utf-8 -*-
"""一次性诊断：看原始 HTML 里图片长什么样、trafilatura 为什么只留一张。"""
import re
import sys
from pathlib import Path

import trafilatura
from selectolax.parser import HTMLParser

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
print("文件大小:", len(raw))
print("<img 出现次数:", len(re.findall(r"<img", raw, re.I)))
print("data-src 次数:", len(re.findall(r"data-src", raw, re.I)))
print("srcset 次数:", len(re.findall(r"srcset", raw, re.I)))
print("sixthtone 图床域名:", set(re.findall(r"https?://[a-z0-9.\-]*sixthtone\.com/[^\"'\s>]{0,60}", raw, re.I)))
print()

tree = HTMLParser(raw)
imgs = tree.css("img")
print("DOM 中 img 元素:", len(imgs))
for i, im in enumerate(imgs[:12]):
    a = im.attributes or {}
    if "sixthtone" in (a.get("src", "") + a.get("data-src", "")) or i < 6:
        print(f"  [{i}] src={a.get('src', '')[:90]}")
        if a.get("data-src"):
            print(f"      data-src={a['data-src'][:90]}")
        if a.get("srcset"):
            print(f"      srcset={a['srcset'][:90]}")
print()

print("--- 正文提取对比 ---")
for label, kw in (
    ("include_images=True", dict(include_images=True)),
    ("include_images=False", dict(include_images=False)),
):
    out = trafilatura.extract(raw, url="https://www.sixthtone.com/news/1019019",
                              output_format="html", include_formatting=True,
                              include_tables=True, include_comments=False, **kw)
    print(f"{label}: len={len(out or '')} <img>={len(re.findall(r'<img', out or '', re.I))}")

print()
print("--- 原始 HTML 前 1200 字符 ---")
print(raw[:1200])
