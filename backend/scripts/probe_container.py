# -*- coding: utf-8 -*-
"""诊断 2：定位站点真实正文容器 + 测试图床 403 的成因。"""
import re
import sys
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
tree = HTMLParser(raw)

# 用正文首句定位承载它的节点，再往上打印祖先的 id/class
needle = sys.argv[2] if len(sys.argv) > 2 else "China will continue to have a relatively abundant workforce"
target = None
for node in tree.css("p, div"):
    if needle[:40] in (node.text() or ""):
        target = node
        break

print("命中节点:", target.tag if target else None)
if target is not None:
    chain, cur, depth = [], target, 0
    while cur is not None and depth < 12:
        a = cur.attributes or {}
        ident = a.get("id") or ""
        cls = a.get("class") or ""
        if ident or cls:
            t = (cur.text() or "")[:70].replace("\n", " ")
            chain.append(f"    {cur.tag:<6} id={ident!r:<22} class={cls[:70]!r}")
        cur = cur.parent
        depth += 1
    print("祖先链（由内向外）:")
    print("\n".join(chain))

print()
print("--- 候选容器统计 ---")
for sel in ["#Content", "div.article", "#div_currpage", "div.content", "article", "#main"]:
    try:
        n = tree.css(sel)
    except Exception as e:
        print(f"  {sel}: 选择器错误 {e}")
        continue
    if n:
        body = (n[0].text() or "")
        print(f"  {sel}: 命中 {len(n)} 个, 首段长度 {len(body)}, 含推荐词={'Chinese Bridge' in body}")

print()
print("--- 图床 403 成因测试 ---")
img = "https://img2.chinadaily.com.cn/images/202609/12/6aa4fa31e4b06d4a392c29f0.jpeg"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
combos = {
    "无 Referer": {"User-Agent": UA},
    "Referer=文章页": {"User-Agent": UA, "Referer": "https://www.chinadaily.com.cn/a/202609/14/WS6aa78d5fe4b06d4aa055dea0.html"},
    "Referer=首页": {"User-Agent": UA, "Referer": "https://www.chinadaily.com.cn/"},
    "Referer=自身": {"User-Agent": UA, "Referer": "https://img2.chinadaily.com.cn/"},
    "无 UA 无 Referer": {},
}
with httpx.Client(follow_redirects=True, timeout=15) as c:
    for label, h in combos.items():
        try:
            r = c.get(img, headers=h)
            print(f"  {label:<18} -> {r.status_code}  {r.headers.get('content-type','')}  {len(r.content)} bytes")
        except Exception as e:
            print(f"  {label:<18} -> EXC {type(e).__name__}: {e}")
