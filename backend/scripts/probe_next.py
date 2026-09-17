# -*- coding: utf-8 -*-
"""诊断 4：Next.js 站点的正文到底藏在 __NEXT_DATA__ 的哪个字段里。

用法：probe_next.py <raw.html>
"""
import json
import re
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
if not m:
    print("没有 __NEXT_DATA__")
    sys.exit(0)

blob = json.loads(m.group(1))

print("=" * 78)
print("[1] 所有含 HTML 标签的长字符串（疑似正文）")
print("=" * 78)
hits = []


def walk(o, path="$"):
    if isinstance(o, dict):
        for k, v in o.items():
            walk(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            walk(v, f"{path}[{i}]")
    elif isinstance(o, str) and len(o) > 150 and re.search(r"<(p|div|br|img)\b", o, re.I):
        hits.append((path, o))


walk(blob)
hits.sort(key=lambda x: -len(x[1]))
for path, s in hits[:10]:
    n_img = len(re.findall(r"<img", s, re.I))
    n_p = len(re.findall(r"<p\b", s, re.I))
    print(f"\n  path : {path}")
    print(f"  len  : {len(s)}   <p>={n_p}   <img>={n_img}")
    print(f"  开头 : {s[:400]}")

print()
print("=" * 78)
print("[2] 结构骨架：含 'content'/'body'/'detail' 的 key 及其类型")
print("=" * 78)


def skim(o, path="$", depth=0):
    if depth > 6:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            kl = k.lower()
            if any(t in kl for t in ("content", "body", "detail", "article", "block", "paragraph", "images")):
                t = type(v).__name__
                extra = ""
                if isinstance(v, str):
                    extra = f"len={len(v)} <img>={len(re.findall(r'<img', v, re.I))}"
                elif isinstance(v, list):
                    extra = f"len={len(v)}"
                print(f"  {path}.{k}  ({t}) {extra}")
            skim(v, f"{path}.{k}", depth + 1)
    elif isinstance(o, list):
        for i, v in enumerate(o[:3]):
            skim(v, f"{path}[{i}]", depth + 1)


skim(blob)

print()
print("=" * 78)
print("[3] 定位正文图片 URL 的完整路径（前 8 个）")
print("=" * 78)
found = []


def dig(o, path="$"):
    if isinstance(o, dict):
        for k, v in o.items():
            dig(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            dig(v, f"{path}[{i}]")
    elif isinstance(o, str) and re.search(r"image5\.sixthtone\.com/image/5/100/\d+\.(jpe?g|png)", o, re.I):
        found.append((path, o))


dig(blob)
for path, u in found[:8]:
    print(f"  {path}\n      {u}")
print(f"\n  合计 {len(found)} 个正文图 URL")

print()
print("=" * 78)
print("[4] __NEXT_DATA__ 顶层结构")
print("=" * 78)
print("  keys:", list(blob.keys()))
props = blob.get("props", {})
print("  props.keys:", list(props.keys()))
pp = props.get("pageProps", {})
print("  pageProps.keys:", list(pp.keys())[:30])
