"""词典释义的用户修正（只改中文释义）。

ECDICT 是只读打开的离线库，偶尔会有释义不对的词条。修正存在主库的
`dict_overrides` 里、**叠在词典之上**：

    查词    `dictionary.lookup()` 命中修正就用修正值，并带上词典原文
            （`meaning_cn_dict`）与 `meaning_edited` 标记 —— 词卡要能「恢复」。
    生词本  `apply` / `clear` 在同一个写事务里把同 lemma 的那一行一起改掉，
            复习卡、导出、检索读的都是 `vocab.meaning_cn`，因此立刻一致。
            新入册的词走 `lemma_entry → lookup`，天然拿到修正值。

键是 `normalize_word` 之后的词形（全小写）。`vocab.lemma` 保留了词典的大小写
（`Tuesday`），所以同步时按 `lower(lemma)` 匹配。

`clear` 需要的词典原文由调用方在事务**之外**查好传进来：只读查询不能叠进
`tx()`（历史上踩过的死锁，见 api/dictionary.py::_enroll）。
"""

from __future__ import annotations

import sqlite3

from ..db import now_iso

MAX_MEANING_CHARS = 500


def get(conn: sqlite3.Connection, word: str) -> str | None:
    row = conn.execute(
        "SELECT meaning_cn FROM dict_overrides WHERE word = ?", (word,)
    ).fetchone()
    return row["meaning_cn"] if row else None


def _sync_vocab(conn: sqlite3.Connection, word: str, meaning_cn: str | None) -> None:
    from .vocab import _fts_sync  # vocab 依赖 dictionary，dictionary 依赖本模块

    ids = [
        int(r["id"])
        for r in conn.execute("SELECT id FROM vocab WHERE lower(lemma) = ?", (word,))
    ]
    if not ids:
        return
    now = now_iso()
    for vid in ids:
        conn.execute(
            "UPDATE vocab SET meaning_cn = ?, updated_at = ? WHERE id = ?",
            (meaning_cn, now, vid),
        )
        # 释义改了要同步 FTS，否则检索结果还指向旧意思
        _fts_sync(conn, vid)


def apply(conn: sqlite3.Connection, word: str, meaning_cn: str) -> None:
    """写入（或覆盖）一条修正，并同步生词本。必须在调用方的写事务里执行。"""
    value = meaning_cn.strip()[:MAX_MEANING_CHARS]
    if not value:
        raise ValueError("修正后的释义不能为空")
    conn.execute(
        "INSERT INTO dict_overrides (word, meaning_cn, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(word) DO UPDATE SET"
        "   meaning_cn = excluded.meaning_cn, updated_at = excluded.updated_at",
        (word, value, now_iso()),
    )
    _sync_vocab(conn, word, value)


def clear(conn: sqlite3.Connection, word: str, dict_meaning: str | None) -> None:
    """删掉修正 = 恢复词典原文，生词本那一行一起恢复。"""
    conn.execute("DELETE FROM dict_overrides WHERE word = ?", (word,))
    _sync_vocab(conn, word, (dict_meaning or "").strip() or None)


__all__ = ["MAX_MEANING_CHARS", "apply", "clear", "get"]
