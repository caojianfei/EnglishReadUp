"""单篇汇总清单的**数据聚合**（需求 6）。

输入是一篇文章的 id，输出是「生词表 + 语法清单」的结构化数据，
渲染成 Markdown / Anki CSV 的活由 `services/export.py` 干。

数据全部来自 M4 之前就已经落库的东西：
  * 生词 ← `vocab_sources`（该篇划过哪些词）JOIN `vocab`（词本身）
  * 语法 ← `grammar_points`
不再为汇总单独跑一次模型 —— 那些内容本来就是模型在每次划词/选段时产出的，
汇总的职责是**聚合与去重**，不是二次创作。理由见 DESIGN §6.5。

这里**没有**「划了线但还没解析」这类中间态：划一段句子不落痕，点过【解析】
才有痕迹与解析记录。清单里的每一行都对应一次真实发生过的解析。
"""

from __future__ import annotations

from ..db import read
from . import dictionary

WORD_COLS = (
    "v.id, v.lemma, v.surface, v.phonetic_uk, v.phonetic_us, v.phonetic_source,"
    " v.phonetic_of, v.pos, v.meaning_cn, v.meaning_en, v.example, v.note,"
    " v.status, v.worth, v.seen_count, s.sentence, s.meaning_in_ctx,"
    " s.article_id, s.highlight_id"
)


def build(article_id: int) -> dict:
    with read() as conn:
        art = conn.execute(
            "SELECT id, title, url, sitename, source_domain, author, published_at,"
            " word_count, status FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        if art is None:
            raise LookupError("文章不存在")

        word_rows = conn.execute(
            f"SELECT {WORD_COLS} FROM vocab_sources s JOIN vocab v ON v.id = s.vocab_id"
            " WHERE s.article_id = ?"
            # 先值得记的，再划过次数多的，最后按首次出现顺序 —— 与生词本页同一套口径
            " ORDER BY v.worth DESC, v.seen_count DESC, s.id ASC",
            (article_id,),
        ).fetchall()

        grammar_rows = conn.execute(
            "SELECT id, category, pattern, explanation_cn, source_sentence,"
            " highlight_id, created_at FROM grammar_points"
            " WHERE article_id = ? ORDER BY id ASC",
            (article_id,),
        ).fetchall()

        hl_total = conn.execute(
            "SELECT COUNT(*) c FROM highlights WHERE article_id = ?", (article_id,)
        ).fetchone()["c"]

    words = []
    for r in word_rows:
        d = dict(r)
        d["phonetic"] = dictionary.primary_phonetic(
            d.get("phonetic_uk"), d.get("phonetic_us")
        )
        d["worth"] = int(d.get("worth") or 0)
        words.append(d)

    grammar = [dict(r) for r in grammar_rows]

    by_cat: dict[str, int] = {}
    for g in grammar:
        cat = (g.get("category") or "其他").strip() or "其他"
        by_cat[cat] = by_cat.get(cat, 0) + 1

    return {
        "article": dict(art),
        "words": words,
        "grammar": grammar,
        "grammar_by_category": by_cat,
        "stats": {
            "words": len(words),
            "words_worth": sum(1 for w in words if w["worth"]),
            "grammar": len(grammar),
            "highlights": hl_total,
        },
    }


# ---------------------------------------------------------------- 全局生词表


def vocab_export_rows(
    article_id: int | None = None, status: str | None = None
) -> list[dict]:
    """导出用的生词行（全局或限定单篇）。

    与 `vocab.list_vocab` 的区别：这里要带上「文中例句」，而且不做分页 ——
    导出的语义就是「全都要」。
    """
    where, params = [], []
    if article_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM vocab_sources s2 WHERE s2.vocab_id = v.id AND s2.article_id = ?)"
        )
        params.append(article_id)
    if status:
        where.append("v.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    with read() as conn:
        rows = conn.execute(
            "SELECT v.id, v.lemma, v.surface, v.phonetic_uk, v.phonetic_us, v.pos,"
            " v.meaning_cn, v.meaning_in_ctx, v.meaning_en, v.example, v.note,"
            " v.status, v.worth, v.seen_count,"
            " (SELECT s.sentence FROM vocab_sources s WHERE s.vocab_id = v.id"
            "   ORDER BY s.id ASC LIMIT 1) AS sentence,"
            " (SELECT s.meaning_in_ctx FROM vocab_sources s WHERE s.vocab_id = v.id"
            "   ORDER BY s.id ASC LIMIT 1) AS source_meaning,"
            " (SELECT a.title FROM vocab_sources s LEFT JOIN articles a ON a.id = s.article_id"
            "   WHERE s.vocab_id = v.id ORDER BY s.id ASC LIMIT 1) AS article_title,"
            " (SELECT GROUP_CONCAT(DISTINCT a2.sitename)"
            "   FROM vocab_sources s3 LEFT JOIN articles a2 ON a2.id = s3.article_id"
            "   WHERE s3.vocab_id = v.id) AS sitenames"
            f" FROM vocab v {clause}"
            " ORDER BY v.worth DESC, v.seen_count DESC, v.id ASC",
            params,
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["phonetic"] = dictionary.primary_phonetic(
            d.get("phonetic_uk"), d.get("phonetic_us")
        )
        d["worth"] = int(d.get("worth") or 0)
        # 导出时用的释义：本义 + 首次出现那句里的语境义（渲染在 export._meaning）
        d["meaning_ctx"] = d.get("source_meaning") or d.get("meaning_in_ctx")
        out.append(d)
    return out
