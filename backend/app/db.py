"""SQLite 连接与初始化。

并发模型：FastAPI 的请求线程与后台抓取线程都会写库。
做法是「每操作一个短连接 + WAL + busy_timeout」，不共享连接对象
（sqlite3 的连接默认禁止跨线程使用），避免引入连接池的复杂度。

## 结构只有 schema.sql 一个来源

M10 起这里**没有任何迁移分支**。以前有一套 `_migrate_*`：给老库补列、去重、
换约束、补历史文章缺的标题块……它们服务的是「已经存在的旧数据库」。

而 data/ 本来就是可再生的（清掉重跑 `scripts/fetch_dicts.py` 就能原地重建），
留着一堆只对历史库有意义的分支，代价是**每次加一列都要在两个地方同步**
（schema.sql 与迁移函数），而且新建的库里永远跑不到的那些代码没人会去验证。
砍掉之后 `init_db()` 只剩一件事：执行 schema.sql。

配套的简化：原本因为「老库要先 ALTER 加列才建得出索引」而推迟到迁移函数里的
三条索引（`idx_vocab_status` / `idx_vocab_due` / `idx_grammar_dedupe`）
已经搬回 schema.sql 里它们该在的位置。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def now_iso() -> str:
    """统一的时间戳格式（UTC，秒精度，便于字符串排序）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_file, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def init_db() -> None:
    """建表。可反复调用（schema.sql 全是 IF NOT EXISTS）。"""
    conn = connect()
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    finally:
        conn.close()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """写事务：正常提交，异常回滚。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
