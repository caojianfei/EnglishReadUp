"""语法点清单（M4）。

语法点的来源只有一处：**解析任务的 `grammar_points` 小节**（M8 起；此前是
独立的 `grammar` 任务，三操作合并后并入解析）。每次解析成功后把那一小节落进
`grammar_points`，单篇汇总直接从这张表读 —— 不再为汇总单独跑一次模型。

**只收 grammar_points，不收 parts**：parts 是「这段怎么读」的逐句拆解
（主干挂靠、从句修饰谁），换个句子就没用了；grammar_points 才是
「换个句子也用得上」的结构条目。混在一起会让清单变成两套口径的杂烩。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from ..db import now_iso, read, tx

log = logging.getLogger("english.grammar")

SELECT_COLS = (
    "g.id, g.article_id, g.highlight_id, g.category, g.pattern,"
    " g.explanation_cn, g.source_sentence, g.created_at"
)

MAX_POINTS_PER_CALL = 6


def _as_str(v: Any) -> str:
    return str(v).strip() if v is not None and not isinstance(v, str) else (v or "").strip()


def persist_points(
    conn: sqlite3.Connection,
    *,
    article_id: int,
    highlight_id: int | None,
    sentence: str,
    points: list[dict],
) -> int:
    """把一次 grammar 调用的结果登记进清单（幂等）。

    必须在调用方的写事务里跑，理由同 vocab.enroll：与 ai_logs 同生共死。
    同篇同结构只留一条 —— 否则同一处划线点两次、或换个划区但命中同一结构，
    清单里就会出现两行一模一样的语法点。
    """
    if not isinstance(points, list):
        return 0

    now = now_iso()
    kept = 0
    for p in points[:MAX_POINTS_PER_CALL]:
        if not isinstance(p, dict):
            continue
        pattern = _as_str(p.get("pattern"))[:200]
        explain_cn = _as_str(p.get("explain_cn")) or _as_str(p.get("explanation_cn"))
        # explanation_cn 是 NOT NULL，没有讲解就没必要占一行
        if not explain_cn:
            continue
        category = normalize_category(p.get("category"))
        evidence = _as_str(p.get("evidence"))[:500] or (sentence or "").strip()[:500] or None

        existing = conn.execute(
            "SELECT id FROM grammar_points WHERE article_id = ?"
            " AND COALESCE(pattern, '') = COALESCE(?, '')",
            (article_id, pattern),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO grammar_points (article_id, highlight_id, category,"
                " pattern, explanation_cn, source_sentence, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (article_id, highlight_id, category, pattern or None,
                 explain_cn, evidence, now),
            )
        else:
            # 已经有了：补空字段，不覆盖已有的讲解（用户可能改过）
            conn.execute(
                "UPDATE grammar_points SET"
                "  category = COALESCE(category, ?),"
                "  explanation_cn = CASE WHEN TRIM(explanation_cn) = '' THEN ? ELSE explanation_cn END,"
                "  source_sentence = COALESCE(source_sentence, ?)"
                " WHERE id = ?",
                (category, explain_cn, evidence, existing["id"]),
            )
        kept += 1
    return kept


def list_points(
    article_id: int | None = None, *, limit: int = 200, offset: int = 0
) -> dict:
    where, params = [], []
    if article_id is not None:
        where.append("g.article_id = ?")
        params.append(article_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM grammar_points g {clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {SELECT_COLS} FROM grammar_points g {clause}"
            f" ORDER BY g.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


def get_point(point_id: int) -> dict | None:
    with read() as conn:
        row = conn.execute(
            f"SELECT {SELECT_COLS} FROM grammar_points g WHERE g.id = ?", (point_id,)
        ).fetchone()
    return dict(row) if row else None


def update_point(
    point_id: int, *, category: str | None = None, explanation_cn: str | None = None
) -> dict | None:
    sets, params = [], []
    if category is not None:
        sets.append("category = ?")
        params.append(category.strip()[:60] or None)
    if explanation_cn is not None:
        txt = explanation_cn.strip()
        if not txt:
            raise ValueError("讲解不能为空")
        sets.append("explanation_cn = ?")
        params.append(txt)
    if not sets:
        return get_point(point_id)
    params.append(point_id)
    with tx() as conn:
        cur = conn.execute(
            f"UPDATE grammar_points SET {', '.join(sets)} WHERE id = ?", params
        )
        if cur.rowcount == 0:
            return None
    return get_point(point_id)


def delete_point(point_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM grammar_points WHERE id = ?", (point_id,))
    return cur.rowcount > 0


def normalize_category(raw: str | None) -> str:
    """把模型自由发挥的类别收敛到设计里那几档，方便分组展示。"""
    txt = (raw or "").strip()
    for known in ("时态", "语态", "从句", "非谓语", "倒装", "虚拟语气", "介词", "搭配"):
        if known in txt:
            return known
    return "其他"


_WS = re.compile(r"\s+")


def pretty_pattern(raw: str | None) -> str:
    return _WS.sub(" ", (raw or "").strip())
