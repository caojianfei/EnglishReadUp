#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实 HTTP 冒烟：拉起 uvicorn 子进程，走网络栈验证，然后关掉。

与 smoke_api.py 的分工：那个用 ASGI 传输测逻辑，这个测「服务真的能起来吗」。
重点看 uvicorn 主进程 + 后台线程池 + Playwright 三者共存是否正常。

用法：python scripts/smoke_http.py [url ...]
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PORT = 8010
BASE = f"http://127.0.0.1:{PORT}"

DEFAULT_URLS = ["https://www.sixthtone.com/news/1018994"]


def main(urls: list[str]) -> int:
    import httpx

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BACKEND),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=BASE, timeout=30) as c:
            for _ in range(40):                      # 等启动
                try:
                    if c.get("/api/health").status_code == 200:
                        break
                except Exception:  # noqa: BLE001
                    time.sleep(0.5)
            else:
                print("[FAIL] uvicorn 未能启动")
                return 1
            print("[PASS] uvicorn 启动，/api/health 可达")

            ids = []
            for u in urls:
                r = c.post("/api/articles", json={"url": u})
                body = r.json()
                ids.append(body["id"])
                print(f"       POST {r.status_code} id={body['id']} status={body['status']}")

            print("[..] 等待抓取完成（渲染站点约 40s/篇）")
            deadline = time.time() + 300
            pending = set(ids)
            while pending and time.time() < deadline:
                for i in list(pending):
                    a = c.get(f"/api/articles/{i}").json()
                    if a["status"] in ("done", "failed"):
                        pending.discard(i)
                        print(f"       #{i} {a['status']} blocks={a['block_count']} "
                              f"imgs={a['image_count']} words={a['word_count']}")
                if pending:
                    time.sleep(4)

            all_done = all(
                c.get(f"/api/articles/{i}").json()["status"] == "done" for i in ids
            )
            print(f"[{'PASS' if all_done else 'FAIL'}] 抓取完成（含 Playwright 渲染路径）")

            # 图片经真实 HTTP 取一次
            d = c.get(f"/api/articles/{ids[0]}").json()
            imgs = [b for b in d["blocks"] if b["type"] == "image" and b["media_url"]]
            if imgs:
                m = c.get(imgs[0]["media_url"])
                ok = m.status_code == 200 and len(m.content) > 1000
                print(f"[{'PASS' if ok else 'FAIL'}] /media 取图 {m.status_code} {len(m.content)}B")
                all_done = all_done and ok

            print(f"[{'PASS' if all_done else 'FAIL'}] 真实 HTTP 端到端")
            return 0 if all_done else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("[done] uvicorn 已停止")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or DEFAULT_URLS))
