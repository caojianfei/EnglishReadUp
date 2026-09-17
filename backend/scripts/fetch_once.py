#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M0 抓取验证脚本 —— app.services.fetcher 的命令行包装。

抓取逻辑本身已在服务模块里（backend/app/services/fetcher.py），本脚本只负责
「批量跑 + 落 Markdown/JSON 供人工验收」，服务端与脚本共用同一套实现。

用法：
    python fetch_once.py <url> [<url> ...]
    python fetch_once.py --out D:/somewhere <url>
    python fetch_once.py --render <url>        # 强制走无头浏览器渲染

产出（默认 backend/data/m0/）：
    raw/<key>.html      原始 HTML（兜底存档）
    assets/<key>/*      本地化后的图片
    <key>.md            最终 Markdown（肉眼验收用）
    <key>.json          blocks + 元数据

验收关注点：
    1. 正文是否干净（无导航/广告/相关阅读）
    2. blocks 顺序与类型是否正确还原原文结构
    3. 图片下载成功率；正文图与图注是否齐全
    4. 是否触发反爬（403 / Cloudflare / 正文过短）
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import export, fetcher  # noqa: E402
from app.services.fetcher import FetchResult  # noqa: E402


def dump(res: FetchResult, url: str, out_root: Path, key: str) -> None:
    rep = res.report
    blocks = [b.to_public() for b in res.blocks]
    meta = {
        "id": key,
        "url": url,
        "canonical_url": rep.canonical_url,
        "title": rep.title,
        "sitename": rep.sitename,
        "author": rep.author,
        "published_at": rep.date,
        "lang": rep.language,
        "word_count": rep.words,
    }
    (out_root / f"{key}.md").write_text(
        export.render_markdown(meta, blocks, media_prefix=f"assets/{key}"), encoding="utf-8"
    )
    (out_root / f"{key}.json").write_text(
        json.dumps(
            {
                "meta": {**meta, "extractor_pass": rep.extractor_pass,
                         "content_selector": rep.content_selector,
                         "image_placement": rep.image_placement},
                "blocks": blocks,
                "report": asdict(rep),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="M0 抓取验证")
    ap.add_argument("urls", nargs="+", help="文章 URL")
    ap.add_argument("--out", default=None, help="输出目录，默认 backend/data/m0")
    ap.add_argument("--render", action="store_true",
                    help="强制所有 URL 走无头浏览器渲染（默认仅配置内的站点走）")
    args = ap.parse_args()

    out_root = Path(args.out).resolve() if args.out else (BACKEND_DIR / "data" / "m0").resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"输出目录 {out_root}", flush=True)

    results: list[tuple[str, FetchResult]] = []
    for i, u in enumerate(args.urls):
        if i:
            fetcher.polite_wait(u)
        key = fetcher.slug_of(u)
        res = fetcher.fetch_article(
            u, asset_key=key, force_render=args.render, log=print, data_root=out_root
        )
        dump(res, u, out_root, key)
        results.append((key, res))

    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    print(f"{'slug':<44}{'status':<9}{'blocks':>7}{'img':>10}{'words':>8}")
    for key, res in results:
        r = res.report
        img_cell = f"{r.images_ok}/{r.images_ok + r.images_failed}"
        print(f"{key[:43]:<44}{r.status:<9}{r.blocks_total:>7}{img_cell:>10}{r.words:>8}")

    print("\n详情")
    for key, res in results:
        r = res.report
        print(f"\n- {key}")
        print(f"  title    : {r.title or '(缺失)'}")
        print(f"  author   : {r.author or '(缺失)'}   date: {r.date or '(缺失)'}")
        print(f"  host     : {r.sitename or '(缺失)'}  lang: {r.language or '(缺失)'}")
        print(f"  selector : {r.content_selector or '(未配置，用通用提取)'}")
        print(f"  extract  : {r.extractor_pass}   {r.image_placement}")
        print(f"  http     : {r.http_status} {r.content_type} {r.html_bytes / 1024:.1f} KB")
        print(f"  blocks   : {r.blocks_by_type}")
        print(f"  images   : {r.images_ok} ok / {r.images_failed} failed / "
              f"{r.images_skipped} 装饰图已丢弃 / {r.images_via_browser} 走浏览器会话")
        print(f"  elapsed  : {r.elapsed_s}s")
        for e in r.errors[:8]:
            print(f"  ! {e}")

    return 0 if all(res.ok for _, res in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
