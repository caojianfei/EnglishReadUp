"""全文检索（需求 5 的「可检索」与需求 6 的回看入口）。

三组结果，对应三张 FTS 表：
  blocks —— 正文原文，「我记得那句话在哪儿」时用
  vocab  —— 生词本的释义与例句
  ai     —— 每一次 AI 解析的输入与输出（需求 5 的兑现场所）

## 为什么每条结果都要带「跳回原文」的位置

检索的价值在于「找到之后能回到现场」。所以每条结果都带上：
  article_id + block_id + start/end（或 highlight_id）
前端据此打开阅读页、滚到那个块、把区间闪一下。没有这些信息的搜索结果
就只是一份文本列表，用户还得自己回去翻。

## 查询串的处理

用户的输入不能直接丢给 FTS5 —— 里面有 `"`、`-`、`*` 会直接报语法错。
这里先抽出字母/数字/汉字片段，再把每个片段做成**带引号的前缀短语**
（`"inva"*`）：引号把 `OR`/`NOT` 这类大写关键词挡在运算符之外，
`*` 让「输入半个词」也能搜到。抽不出任何片段就返回空，不报错。

注意前缀匹配撞的是 **porter 词干**（三张表都是 `porter unicode61`）：
搜 `sitt` 匹配不到索引里的 `sit`，得搜完整的 `sitting` 或 `sit` ——
这是分词器的正常行为，不是缺陷。

中文检索另走 LIKE 兜底：unicode61 不切分汉字，整串会被当成一个词。
"""

from __future__ import annotations

import re
import sqlite3

from ..db import read
from . import dictionary

SNIPPET_TOKENS = 14
_TOKEN = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+")
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def fts_query(q: str) -> str | None:
    toks = _TOKEN.findall(q or "")
    if not toks:
        return None
    # 多个片段按 AND 连接（FTS5 里空格即 AND）。
    #
    # 每个片段都写成**带引号的前缀短语**（`"inva"*`）而不是裸的 `inva*`：
    # 裸写法会把 `OR` / `NOT` / `NEAR` 这类**大写**关键词当成 FTS5 运算符，
    # 用户搜「a OR b」或「NOT x」就直接 500（M4 实测踩到）。加引号后它们
    # 退化成普通词，既不会炸也更符合用户的原意 —— 他要搜的是那个词。
    return " ".join(f'"{t}"*' for t in toks)


def _like(q: str) -> str:
    return f"%{q.strip()}%"


def _marked(*snips: str | None) -> str | None:
    """挑出真正含命中的那一段。

    FTS5 的 `snippet()` 对**每一列**都返回一段文本，没命中的那一列返回的是
    开头若干 token —— 拿错列就会出现「搜 mural，结果展示的是 JSON 的
    `{"translation": ...`」这种看不出命中在哪的片段。
    所以先找带 `<mark>` 的，全都带或不带时才按下标先后退化。
    """
    for s in snips:
        if s and "<mark>" in s:
            return s
    for s in snips:
        if s:
            return s
    return None


def _first_span(text: str, q: str) -> tuple[int, int] | None:
    """在 blocks.text 里定位第一个命中的字符区间。

    FTS 只告诉我们「这个块命中了」，不告诉我们在哪一位。前端要滚到准确位置、
    把那一小段闪一下，就得自己找。找不到（比如只命中了词干变体）就返回 None，
    前端退化成「滚到块首」。
    """
    if not text:
        return None
    low, ql = text.lower(), (q or "").strip().lower()
    if not ql:
        return None
    at = low.find(ql)
    if at >= 0:
        return at, at + len(ql)
    # 用最长的一个片段再试一次：用户可能输了一串词，但正文里只出现其中一个
    toks = sorted(_TOKEN.findall(ql), key=len, reverse=True)
    for t in toks:
        at = low.find(t)
        if at >= 0:
            return at, at + len(t)
    return None


# ---------------------------------------------------------------- 三组检索


def search_blocks(conn: sqlite3.Connection, q: str, limit: int) -> list[dict]:
    match = fts_query(q)
    rows = []
    if match:
        rows = conn.execute(
            "SELECT f.block_id, f.article_id, b.text, a.title,"
            " snippet(fts_blocks, 0, '<mark>', '</mark>', '…', ?) AS snip"
            " FROM fts_blocks f"
            " JOIN blocks b ON b.id = f.block_id"
            " JOIN articles a ON a.id = f.article_id"
            " WHERE fts_blocks MATCH ? AND b.is_stale = 0"
            " ORDER BY rank LIMIT ?",
            (SNIPPET_TOKENS, match, limit),
        ).fetchall()

    out = []
    for r in rows:
        span = _first_span(r["text"] or "", q)
        out.append({
            "article_id": r["article_id"],
            "block_id": r["block_id"],
            "article_title": r["title"],
            "snippet": r["snip"],
            "start": span[0] if span else None,
            "end": span[1] if span else None,
        })
    return out


def search_vocab(conn: sqlite3.Connection, q: str, limit: int) -> list[dict]:
    match = fts_query(q)
    rows = []
    if match:
        rows = conn.execute(
            "SELECT f.vocab_id, snippet(fts_vocab, 0, '<mark>', '</mark>', '…', 10) AS snip,"
            " snippet(fts_vocab, 1, '<mark>', '</mark>', '…', 14) AS snip_meaning"
            " FROM fts_vocab f WHERE fts_vocab MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()

    ids = [r["vocab_id"] for r in rows]
    if not ids:
        # 汉字或词形变化导致的漏召回：退回子串匹配
        like = _like(q)
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM vocab WHERE lemma LIKE ? OR meaning_cn LIKE ?"
                " OR meaning_in_ctx LIKE ? ORDER BY worth DESC, seen_count DESC LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
        ]
        rows = [{"vocab_id": i, "snip": None, "snip_meaning": None} for i in ids]

    # 注意转成 dict：FTS 那条路径给的是 sqlite3.Row（只有下标访问，没有 .get），
    # LIKE 兜底那条给的是普通 dict —— 两种混在一起，统一成 dict 才不会炸。
    snips = {r["vocab_id"]: dict(r) for r in rows}
    out = []
    for vid in ids:
        v = conn.execute(
            "SELECT id, lemma, surface, phonetic_uk, phonetic_us, pos, meaning_cn,"
            " meaning_in_ctx, status, worth, seen_count,"
            " (SELECT s.sentence FROM vocab_sources s WHERE s.vocab_id = v.id"
            "   ORDER BY s.id ASC LIMIT 1) AS sentence,"
            " (SELECT GROUP_CONCAT(DISTINCT a2.title) FROM vocab_sources s2"
            "   LEFT JOIN articles a2 ON a2.id = s2.article_id"
            "   WHERE s2.vocab_id = v.id) AS article_titles"
            " FROM vocab v WHERE v.id = ?",
            (vid,),
        ).fetchone()
        if v is None:
            continue
        d = dict(v)
        d["phonetic"] = dictionary.primary_phonetic(
            d.get("phonetic_uk"), d.get("phonetic_us")
        )
        d["worth"] = int(d.get("worth") or 0)
        s = snips.get(vid) or {}
        d["snippet"] = _marked(s.get("snip"), s.get("snip_meaning")) or d.get("meaning_cn")
        out.append(d)
    return out


def search_ai(conn: sqlite3.Connection, q: str, limit: int) -> list[dict]:
    match = fts_query(q)
    rows = []
    if match:
        rows = conn.execute(
            "SELECT l.id, l.article_id, l.prompt_type, l.prompt_version, l.model,"
            " l.highlight_id, l.input_text, l.created_at,"
            " snippet(fts_ai, 0, '<mark>', '</mark>', '…', 14) AS snip_in,"
            " snippet(fts_ai, 1, '<mark>', '</mark>', '…', 14) AS snip_out,"
            " a.title AS article_title"
            " FROM fts_ai f"
            " JOIN ai_logs l ON l.id = f.ai_log_id"
            " LEFT JOIN articles a ON a.id = l.article_id"
            " WHERE fts_ai MATCH ? AND l.prompt_type != 'summarize'"
            " ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()

    if not rows:
        like = _like(q)
        rows = conn.execute(
            "SELECT l.id, l.article_id, l.prompt_type, l.prompt_version, l.model,"
            " l.highlight_id, l.input_text, l.created_at, NULL AS snip_in,"
            " NULL AS snip_out, a.title AS article_title"
            " FROM ai_logs l LEFT JOIN articles a ON a.id = l.article_id"
            " WHERE l.output_json LIKE ? AND l.prompt_type != 'summarize'"
            " ORDER BY l.id DESC LIMIT ?",
            (like, limit),
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        # 跳回原文：痕迹还在就带上块与区间，否则只能回到文章
        d["block_id"] = None
        d["start"] = None
        d["end"] = None
        d["surface"] = None
        if d["highlight_id"]:
            h = conn.execute(
                "SELECT block_id, start_offset, end_offset, surface FROM highlights WHERE id = ?",
                (d["highlight_id"],),
            ).fetchone()
            if h:
                d["block_id"] = h["block_id"]
                d["start"] = h["start_offset"]
                d["end"] = h["end_offset"]
                d["surface"] = h["surface"]
        d["snippet"] = _marked(d.get("snip_out"), d.get("snip_in")) or d.get("input_text")
        out.append(d)
    return out


def search_all(q: str, *, limit: int = 20) -> dict:
    q = (q or "").strip()
    if not q:
        return {"q": q, "total": 0, "blocks": [], "vocab": [], "ai": []}
    with read() as conn:
        blocks = search_blocks(conn, q, limit)
        vocab = search_vocab(conn, q, limit)
        ai = search_ai(conn, q, limit)
    return {
        "q": q,
        "total": len(blocks) + len(vocab) + len(ai),
        "blocks": blocks,
        "vocab": vocab,
        "ai": ai,
    }


def suggest_vocab(q: str, limit: int = 8) -> list[dict]:
    """前缀补全用：只回词形，够轻。"""
    like = f"{q.strip()}%"
    with read() as conn:
        rows = conn.execute(
            "SELECT id, lemma, phonetic_uk, phonetic_us, meaning_cn FROM vocab"
            " WHERE lemma LIKE ? OR surface LIKE ?"
            " ORDER BY worth DESC, seen_count DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["phonetic"] = dictionary.primary_phonetic(
            d.get("phonetic_uk"), d.get("phonetic_us")
        )
        out.append(d)
    return out
