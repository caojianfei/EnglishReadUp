#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端冒烟：不启动 uvicorn，用 ASGI 传输直接打真实的 FastAPI 应用。

覆盖：提交 URL（含幂等重复提交）→ 轮询后台任务 → 校验 blocks/图片/图注
→ 图片可访问 → Markdown 导出 → FTS 检索 → 重抓不丢 block id → 删除。

用法：python scripts/smoke_api.py [url ...]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

DEFAULT_URLS = [
    "https://www.chinadaily.com.cn/a/202609/11/WS6aa35187e4b06d4aa055d756.html",
    "https://www.sixthtone.com/news/1018994",
]

PASS, FAIL = "PASS", "FAIL"
checks: list[tuple[str, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    checks.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name} {detail}", flush=True)
    return ok


async def wait_done(c: httpx.AsyncClient, ids: list[int], timeout_s: int = 360) -> dict[int, dict]:
    deadline = time.time() + timeout_s
    pending = set(ids)
    out: dict[int, dict] = {}
    while pending and time.time() < deadline:
        for i in list(pending):
            a = (await c.get(f"/api/articles/{i}")).json()
            if a["status"] in ("done", "failed"):
                pending.discard(i)
                out[i] = a
                print(f"    #{i} -> {a['status']} blocks={a['block_count']} "
                      f"imgs={a['image_count']} words={a['word_count']} "
                      f"{(a.get('error_msg') or '')[:120]}", flush=True)
        if pending:
            await asyncio.sleep(4)
    return out


async def main(urls: list[str]) -> int:
    init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://smoke", timeout=60) as c:
        h = (await c.get("/api/health")).json()
        check(h["status"] == "ok", "health", f"render_domains={h['render_domains']}")

        print("\n[1] 提交 URL")
        ids: list[int] = []
        for u in urls:
            r = await c.post("/api/articles", json={"url": u})
            body = r.json()
            ids.append(body["id"])
            print(f"    POST {r.status_code} id={body['id']} status={body['status']} {u}")
        check(len(ids) == len(urls) and all(ids), "全部入队", str(ids))

        print("\n[2] 重复提交幂等")
        r2 = await c.post("/api/articles", json={"url": urls[0]})
        check(r2.json()["id"] == ids[0], "同一 URL 不新建文章", f"id={r2.json()['id']}")

        print("\n[3] 非法 URL 拒绝")
        r3 = await c.post("/api/articles", json={"url": "not-a-url"})
        check(r3.status_code == 422, "非法 URL 返回 422", f"got {r3.status_code}")

        print("\n[4] 等待后台抓取")
        done = await wait_done(c, ids)
        check(len(done) == len(ids), "全部抓取结束", f"{len(done)}/{len(ids)}")
        check(all(a["status"] == "done" for a in done.values()), "全部成功")

        print("\n[5] 详情与内容校验")
        for i in ids:
            d = (await c.get(f"/api/articles/{i}")).json()
            types: dict[str, int] = {}
            for b in d["blocks"]:
                types[b["type"]] = types.get(b["type"], 0) + 1
            print(f"\n    #{i} 《{d['title']}》")
            print(f"       blocks={types}  words={d['word_count']}  pass={d['extractor_pass']}")
            check(d["block_count"] > 3, f"#{i} 有正文块", str(d["block_count"]))
            check(not any(b["is_stale"] for b in d["blocks"]), f"#{i} 无 stale 块")

            imgs = [b for b in d["blocks"] if b["type"] == "image"]
            owned = [b for b in imgs if b["media_url"]]
            print(f"       images={len(imgs)} 已本地化={len(owned)}")
            for b in imgs[:3]:
                cap = next((x["text"] for x in d["blocks"]
                            if x["type"] == "caption" and x["order_index"] == b["order_index"] + 1), "")
                alt = (b.get("alt") or "")[:40]
                print(f"         - {b['media_url']}  alt={alt!r}  cap={(cap or '')[:60]!r}")

            if owned:
                m = await c.get(owned[0]["media_url"])
                check(m.status_code == 200 and len(m.content) > 1000,
                      f"#{i} 图片可访问", f"{m.status_code} {len(m.content)}B")

            md = await c.get(f"/api/articles/{i}/markdown")
            check(md.status_code == 200 and md.text.startswith("# "),
                  f"#{i} Markdown 导出", f"{len(md.text)} chars")

        print("\n[6] 重抓不丢 block id（学习痕迹的前提）")
        before = {b["id"]: b["type"] for b in (await c.get(f"/api/articles/{ids[0]}")).json()["blocks"]}
        await c.post(f"/api/articles/{ids[0]}/refetch")
        await wait_done(c, [ids[0]])
        after = {b["id"]: b["type"] for b in (await c.get(f"/api/articles/{ids[0]}")).json()["blocks"]}
        overlap = len(set(before) & set(after))
        check(overlap == len(after) and len(after) > 0,
              "重抓后 block id 全部复用", f"{overlap}/{len(after)} 复用（旧 {len(before)}）")

        print("\n[7] FTS 检索（M4 起三组一起返回：正文 / 生词本 / AI 记录）")
        s = (await c.get("/api/search", params={"q": "the"})).json()
        check(s["total"] > 0, "检索命中", f"{s['total']} 条")
        check(s["total"] == len(s["blocks"]) + len(s["vocab"]) + len(s["ai"]),
              "total = 三组之和", f"b{len(s['blocks'])}+v{len(s['vocab'])}+a{len(s['ai'])}")
        check(len(s["blocks"]) > 0, "正文组命中", f"{len(s['blocks'])} 条")
        for hit in s["blocks"][:3]:
            check(bool(hit["article_id"] and hit["block_id"]), "正文命中带跳转位置",
                  f"blk={hit['block_id']}")
            print(f"       #{hit['article_id']} {(hit['article_title'] or '')[:40]!r}"
                  f" … {(hit['snippet'] or '')[:80]!r}")
        # FTS5 关键词（大写 OR/NOT）不能把查询语法搞崩：以前会 500
        weird = await c.get("/api/search", params={"q": "a OR b NOT c"})
        check(weird.status_code == 200, "含 FTS5 关键词的查询不报错",
              f"HTTP {weird.status_code}")

        print("\n[7.5] 阅读进度（M2）")
        pid = ids[0]
        p = (await c.get(f"/api/articles/{pid}/progress")).json()
        check(p["article_id"] == pid, "进度接口可用", f"id={p['article_id']}")
        # read_seconds 是累加值，跑过几轮之后不会归零 —— 所以断言一律用相对增量
        base = p["read_seconds"]
        blk = (await c.get(f"/api/articles/{pid}")).json()["blocks"]
        mid = blk[len(blk) // 2]["id"]
        p1 = (await c.put(f"/api/articles/{pid}/progress",
                          json={"last_block_id": mid, "scroll_pct": 0.42, "read_seconds": 30})).json()
        check(p1["last_block_id"] == mid and abs(p1["scroll_pct"] - 0.42) < 1e-6
              and p1["read_seconds"] == base + 30,
              "上报落库（位置 + 增量秒数）", f"seconds {base}→{p1['read_seconds']}")
        p2 = (await c.put(f"/api/articles/{pid}/progress", json={"read_seconds": 12})).json()
        check(p2["read_seconds"] == base + 42 and p2["last_block_id"] == mid,
              "增量累加、位置不被空值冲掉", f"read_seconds={p2['read_seconds']}")
        p3 = (await c.put(f"/api/articles/{pid}/progress",
                          json={"last_block_id": 99999999, "read_seconds": 0})).json()
        check(p3["last_block_id"] == mid, "非法 block_id 被忽略", str(p3["last_block_id"]))
        p4 = (await c.put(f"/api/articles/{pid}/progress", json={"scroll_pct": 1.5}))
        check(p4.status_code == 422, "scroll_pct 越界拒绝", f"got {p4.status_code}")
        row = (await c.get(f"/api/articles/{pid}")).json()
        check(row["last_block_id"] == mid and row["read_seconds"] == base + 42,
              "详情接口带出进度（列表页复用同一 join）", f"pct={row['scroll_pct']}")

        print("\n[8] 409 并发保护 / 404")
        r409 = await c.post(f"/api/articles/{ids[0]}/refetch")
        r404 = await c.get("/api/articles/999999")
        check(r404.status_code == 404, "不存在的文章 404")
        print(f"       重复 refetch → {r409.status_code}（409 表示已在跑，200 表示抓得快）")

        print("\n[9] 删除")
        # 自建自删：用一条注定抓不到正文的 URL 走完「提交 → 失败 → 删除」，
        # 这样不会把上面两篇样例文章从库里删掉（M2 的 UI 验收还要用它们）。
        tmp = await c.post("/api/articles", json={"url": "https://example.com/smoke-delete-me"})
        tid = tmp.json()["id"]
        await wait_done(c, [tid], timeout_s=90)
        rd = await c.delete(f"/api/articles/{tid}")
        rg = await c.get(f"/api/articles/{tid}")
        check(rd.status_code == 204 and rg.status_code == 404, "删除生效（自建自删）", f"id={tid}")

    ok = sum(1 for s, _, _ in checks if s == PASS)
    print(f"\n{'=' * 60}\n结果：{ok}/{len(checks)} 通过")
    for s, name, detail in checks:
        if s == FAIL:
            print(f"  [FAIL] {name} {detail}")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    urls = sys.argv[1:] or DEFAULT_URLS
    raise SystemExit(asyncio.run(main(urls)))
