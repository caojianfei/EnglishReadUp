"""学习痕迹：划词与选段。

红线 1：痕迹表与原文结构表分离，重抓文章不影响这里。
红线 2：只存「块 id + 字符区间」，不存 HTML，也不把高亮写进正文 ——
        渲染时由区间重建。这样原文重抓后坐标依然有效
        （因为 blocks.text 是不可变的纯文本，重抓命中 block_uid 时 id 也不变）。

`surface` 的可信来源是 `blocks.text[start:end]` 而不是前端传来的字符串：
前端做跨节点选区时偏移偶有偏差，以正文切片为准可以自动纠正；
差异过大则判定为前端算错，直接拒绝，避免写进错位的痕迹。
"""

from __future__ import annotations

import logging
import re

from ..db import now_iso, read, row_to_dict, tx

log = logging.getLogger("english.trace")

HIGHLIGHT_COLS = (
    "h.id, h.article_id, h.block_id, h.start_offset, h.end_offset, h.kind,"
    " h.surface, h.context_before, h.context_after, h.note, h.color, h.created_at,"
    " b.type AS block_type, b.order_index"
)

MAX_SPAN = 4000          # 单条痕迹的字符上限，防止误选整篇
MAX_SURFACE_DRIFT = 24   # 前端 surface 与正文切片的允许偏差（用于跨节点选区时的小误差）
_LETTER = re.compile(r"[A-Za-z]")   # 「算得上一个词」的最低门槛，见 infer_kind


class HighlightError(ValueError):
    """入参不合法（块不属于该文章、区间越界等）。"""


def infer_kind(surface: str, given: str | None = None) -> str:
    """推断选区类别。前端可以指定，但后端复核 —— 前端只知道用户拖了什么，
    不知道 `blocks.text` 的确切边界。

    「一个词」的门槛是**有一个 token 且含字母**：`1929`、`——`、`…` 划中也查不出
    什么来，判成 word 会让它们进生词本。口径与前端 `lib/selection.ts::inferKind`
    刻意保持一致（前端还会先把词内片段吸附到整词，见 `snapToWholeWord`）。
    """
    s = surface.strip()
    if given in ("word", "phrase", "sentence", "paragraph"):
        # 复核：单个词被标成 paragraph 之类显然不合理，纠正之
        if given == "word" and (len(s.split()) > 1 or not _LETTER.search(s)):
            given = "phrase"
        elif given == "paragraph" and len(s) < 200:
            given = "sentence" if s.endswith((".", "?", "!")) else "phrase"
        return given
    if not s:
        return "phrase"
    if len(s) > 240:
        return "paragraph"
    if len(s.split()) == 1:
        return "word" if _LETTER.search(s) else "phrase"
    if s.endswith((".", "?", "!", ".\"", "?”")) and len(s) > 30:
        return "sentence"
    return "phrase"


def create_highlight(
    article_id: int,
    block_id: int,
    start_offset: int,
    end_offset: int,
    *,
    kind: str | None = None,
    surface: str | None = None,
    color: str = "yellow",
    note: str | None = None,
) -> dict:
    """创建一条划词痕迹。同一区间重复划词会复用已有记录（幂等）。"""
    if start_offset < 0 or end_offset <= start_offset:
        raise HighlightError("区间不合法")
    if end_offset - start_offset > MAX_SPAN:
        raise HighlightError(f"选区过长（上限 {MAX_SPAN} 字符）")

    with tx() as conn:
        row = conn.execute(
            "SELECT text FROM blocks WHERE id = ? AND article_id = ?",
            (block_id, article_id),
        ).fetchone()
        if row is None:
            raise HighlightError("该块不属于这篇文章")
        text = row["text"] or ""

        if end_offset > len(text):
            raise HighlightError(
                f"区间越界：块文本长度 {len(text)}，请求 {start_offset}~{end_offset}"
            )
        actual = text[start_offset:end_offset]
        if not actual.strip():
            raise HighlightError("选区内没有文字")

        # 前端传的 surface 与正文切片对不上时，以正文为准；
        # 差得太多说明偏移算错了，宁可报错也不要写错位的痕迹。
        if surface and surface.strip() != actual.strip():
            if abs(len(surface) - len(actual)) > MAX_SURFACE_DRIFT:
                raise HighlightError(
                    f"选区与正文不一致（前端 {len(surface)} 字 / 正文 {len(actual)} 字）"
                )
            log.debug("surface 已按正文切片纠正：%r → %r", surface[:40], actual[:40])

        kind_final = infer_kind(actual, kind)

        # 幂等：同块同区间已存在则直接返回，避免重复划词堆记录
        dup = conn.execute(
            "SELECT id FROM highlights WHERE block_id = ? AND start_offset = ?"
            " AND end_offset = ?",
            (block_id, start_offset, end_offset),
        ).fetchone()
        if dup:
            hid = int(dup["id"])
            if note:
                conn.execute("UPDATE highlights SET note = ? WHERE id = ?", (note, hid))
        else:
            cur = conn.execute(
                "INSERT INTO highlights (article_id, block_id, start_offset, end_offset,"
                " kind, surface, context_before, context_after, note, color, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    article_id, block_id, start_offset, end_offset, kind_final,
                    actual, text[max(0, start_offset - 160):start_offset],
                    text[end_offset:end_offset + 160], note, color, now_iso(),
                ),
            )
            hid = int(cur.lastrowid)
        return _get(conn, hid)


def _get(conn, highlight_id: int) -> dict:
    row = conn.execute(
        f"SELECT {HIGHLIGHT_COLS} FROM highlights h"
        f" LEFT JOIN blocks b ON b.id = h.block_id WHERE h.id = ?",
        (highlight_id,),
    ).fetchone()
    d = dict(row)
    # 供前端做「围绕痕迹的上下文预览」
    d["preview"] = (d.get("context_before") or "")[-80:] + d["surface"] \
        + (d.get("context_after") or "")[:80]
    return d


def get_highlight(highlight_id: int) -> dict | None:
    with read() as conn:
        row = conn.execute("SELECT 1 FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
        return _get(conn, highlight_id) if row else None


def list_highlights(
    article_id: int | None = None,
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    where, params = [], []
    if article_id is not None:
        where.append("h.article_id = ?")
        params.append(article_id)
    if kind:
        where.append("h.kind = ?")
        params.append(kind)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM highlights h {clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {HIGHLIGHT_COLS} FROM highlights h"
            f" LEFT JOIN blocks b ON b.id = h.block_id {clause}"
            f" ORDER BY h.block_id, h.start_offset LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["preview"] = (d.get("context_before") or "")[-80:] + d["surface"] \
            + (d.get("context_after") or "")[:80]
        items.append(d)
    return {"total": total, "items": items}


def update_highlight(
    highlight_id: int, *, note: str | None = None, color: str | None = None
) -> dict | None:
    sets, params = [], []
    if note is not None:
        sets.append("note = ?")
        params.append(note[:2000] or None)
    if color is not None:
        sets.append("color = ?")
        params.append(color[:20])
    if not sets:
        return get_highlight(highlight_id)
    with tx() as conn:
        exists = conn.execute(
            "SELECT 1 FROM highlights WHERE id = ?", (highlight_id,)
        ).fetchone()
        if not exists:
            return None
        conn.execute(
            f"UPDATE highlights SET {', '.join(sets)} WHERE id = ?",
            [*params, highlight_id],
        )
        return _get(conn, highlight_id)


def delete_highlight(highlight_id: int) -> bool:
    """删除一条痕迹。

    ai_logs.highlight_id 是 ON DELETE SET NULL，所以 AI 解析记录会保留
    （用户删高亮不等于要抹掉学习历史），只是不再挂在某条高亮上。
    """
    with tx() as conn:
        cur = conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
        return cur.rowcount > 0


def counts_by_kind(article_id: int) -> dict[str, int]:
    with read() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) c FROM highlights WHERE article_id = ? GROUP BY kind",
            (article_id,),
        ).fetchall()
    return {r["kind"]: int(r["c"]) for r in rows}


__all__ = [
    "HighlightError",
    "counts_by_kind",
    "create_highlight",
    "delete_highlight",
    "get_highlight",
    "infer_kind",
    "list_highlights",
    "row_to_dict",
    "update_highlight",
]
