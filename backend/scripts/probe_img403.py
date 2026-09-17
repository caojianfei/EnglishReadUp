# -*- coding: utf-8 -*-
"""诊断 9：China Daily 图床 403 —— 试三种浏览器内下载方式。"""
import base64
import sys

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
article = "https://www.chinadaily.com.cn/a/202609/11/WS6aa35187e4b06d4aa055d756.html"
imgs = [
    "https://img2.chinadaily.com.cn/images/202609/11/6aa35186e4b06d4a392c0ec5.jpeg",
    "https://img2.chinadaily.com.cn/images/202609/11/6aa35186e4b06d4a392c0ec7.jpeg",
]

FETCH_JS = r"""
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) return {ok: false, status: r.status};
    const buf = await r.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return {ok: true, status: r.status, b64: btoa(bin), size: bytes.length};
  } catch (e) {
    return {ok: false, error: String(e)};
  }
}
"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page()
    pg.goto(article, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(3000)
    for _ in range(4):
        pg.mouse.wheel(0, 1500)
        pg.wait_for_timeout(400)

    for u in imgs:
        print("=" * 74)
        print("目标:", u)

        # 方式 1：浏览器内 fetch
        res = pg.evaluate(FETCH_JS, u)
        if res.get("ok"):
            raw = base64.b64decode(res["b64"])
            print(f"  [1] 浏览器内 fetch      -> OK  {res['status']}  {len(raw)} bytes  "
                  f"magic={raw[:4].hex()}")
        else:
            print(f"  [1] 浏览器内 fetch      -> 失败 {res}")

        # 方式 2：context.request（复用 cookie）
        try:
            r = ctx.request.get(u, headers={"Referer": article})
            print(f"  [2] context.request     -> {r.status}  {len(r.body())} bytes")
        except Exception as e:
            print(f"  [2] context.request     -> EXC {e}")

        # 方式 3：页面直接用 img 请求 URL 取自然尺寸（判断浏览器能否显示）
        dim = pg.evaluate(
            """(url) => new Promise(res => {
                 const i = new Image();
                 i.onload = () => res({ok: true, w: i.naturalWidth, h: i.naturalHeight});
                 i.onerror = () => res({ok: false});
                 i.src = url;
                 setTimeout(() => res({ok: false, timeout: true}), 8000);
               })""", u)
        print(f"  [3] 页面 <img> 加载      -> {dim}")

    b.close()
