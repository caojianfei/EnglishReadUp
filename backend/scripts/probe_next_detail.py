# -*- coding: utf-8 -*-
"""诊断 5：textImageList 能否定位到正文中的插入位置。

用法：probe_next_detail.py <raw.html>
"""
import json
import re
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
blob = json.loads(re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S).group(1))
data = blob["props"]["pageProps"]["detailData"]["data"]

print("=" * 78)
print("[1] detailData.data 全部字段")
print("=" * 78)
for k, v in data.items():
    t = type(v).__name__
    if isinstance(v, str):
        info = f"len={len(v)}  <img>={len(re.findall(r'<img', v, re.I))}"
        info += f"\n       值: {v[:160]!r}"
    elif isinstance(v, list):
        info = f"len={len(v)}"
        if v and isinstance(v[0], dict):
            info += f"  元素 keys={list(v[0].keys())}"
    elif isinstance(v, dict):
        info = f"keys={list(v.keys())[:14]}"
    else:
        info = repr(v)[:120]
    print(f"  {k:<22} ({t}) {info}")

print()
print("=" * 78)
print("[2] textImageList 完整元素（前 3 个 + 最后 1 个）")
print("=" * 78)
tl = data.get("textImageList") or []
print(f"  共 {len(tl)} 个")
for i in list(range(min(3, len(tl)))) + ([len(tl) - 1] if len(tl) > 3 else []):
    print(f"\n  --- [{i}] ---")
    print(json.dumps(tl[i], ensure_ascii=False, indent=4)[:1200])

print()
print("=" * 78)
print("[3] 正文正文与图片的相对位置线索")
print("=" * 78)
content = data.get("content") or ""
print(f"  content 中 <img> 数: {len(re.findall(r'<img', content, re.I))}")
print(f"  content 中图片占位符候选: {re.findall(r'<figure[^>]*>|<!--[^>]{0,60}-->|\[image\]|__IMG__', content, re.I)[:10]}")
paras = re.findall(r"<p\b[^>]*>(.*?)</p>", content, re.S | re.I)
print(f"  段落数: {len(paras)}")
print("  各段落首 60 字（用于和 textImageList 的 caption 对齐）：")
for i, p in enumerate(paras):
    txt = re.sub(r"<[^>]+>", "", p).strip()
    print(f"    [{i:>2}] {txt[:60]}")
