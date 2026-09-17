#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把库里的文章导出成 Markdown 文件（离线阅读 / 二次加工 / 人工验收）。

图片默认指向 `/media/...`，配合运行中的服务可直接预览；
加 `--local` 则写成 `assets/{id}/xxx.jpg` 相对路径，交由 M0 脚本产物目录阅读。

用法：
    python scripts/export_md.py 2
    python scripts/export_md.py --all
    python scripts/export_md.py --all --local
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.db import init_db, read  # noqa: E402
from app.services import export, ingest  # noqa: E402


def _all_ids() -> list[int]:
    with read() as conn:
        return [int(r["id"]) for r in conn.execute(
            "SELECT id FROM articles WHERE status='done' ORDER BY id"
        ).fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="导出文章为 Markdown")
    ap.add_argument("ids", nargs="*", type=int, help="文章 id")
    ap.add_argument("--all", action="store_true", help="导出全部已完成的文章")
    ap.add_argument("--out", default=None, help="输出目录，默认 backend/data/exports")
    ap.add_argument("--local", action="store_true", help="图片写相对路径而非 /media 路由")
    args = ap.parse_args()

    init_db()
    ids = _all_ids() if args.all else args.ids
    if not ids:
        print("没有可导出的文章（用 --all 或指定 id）")
        return 1

    out_dir = Path(args.out).resolve() if args.out else (settings.data_dir / "exports")
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in ids:
        art = ingest.get_article(i)
        if art is None:
            print(f"#{i} 不存在，跳过")
            continue
        blocks = ingest.get_blocks(i)
        prefix = None if args.local else f"/media/{i}"
        md = export.render_markdown(art, blocks, media_prefix=prefix)
        path = out_dir / f"article-{i}.md"
        path.write_text(md, encoding="utf-8")
        print(f"#{i} {art['status']:<7} blocks={len(blocks):<4} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
