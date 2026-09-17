#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开发自检：初始化数据库、打印表与路由清单。

用法：python scripts/dev_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from app.config import settings
    from app.db import connect, init_db

    init_db()
    print("data_dir ->", settings.data_dir)
    print("db       ->", settings.db_file)

    with connect() as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        ).fetchall()]
        print("tables   ->", ", ".join(tables))
        print("fts5     ->", "fts_blocks" in tables)

    from app.main import app

    # FastAPI ≥0.141 用 _IncludedRouter 惰性展开，直接遍历 app.routes 看不到子路由，
    # 因此从 OpenAPI 文档取真实路径。
    print("\nroutes:")
    for path, ops in sorted(app.openapi()["paths"].items()):
        methods = ",".join(sorted(m.upper() for m in ops))
        print(f"  {methods:<14} {path}")

    from app.services import export, fetcher, ingest, jobs  # noqa: F401

    print("\nimports ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
