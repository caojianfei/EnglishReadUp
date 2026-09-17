"""导出：把库里的内容渲染成可带走的文件。

两件事：
  1. 单篇正文 → Markdown（`/api/articles/{id}/markdown`），人工验收 / 二次加工用；
  2. 学习成果 → 汇总清单 Markdown 与 Anki CSV（`/api/articles/{id}/export`、`/api/vocab/export`）。

所有渲染都是**纯函数**：输入是 `summarize` 聚合出来的 dict，输出是字符串。
需要落盘或流式返回由 API 层决定，这里不碰 IO —— 好测、可复现。
"""

from __future__ import annotations

import csv
import io
import re
from html import escape
from urllib.parse import urlparse


def _img_url(row: dict, media_prefix: str | None) -> str:
    """图片一律走本地路径，保证导出后不依赖原站（防盗链、失效都无所谓）。

    media_prefix 为空时返回 local_path 原样（离线 Markdown 用相对路径），
    否则拼成 {media_prefix}/{文件名}（服务端走 /media 路由）。
    """
    lp = row.get("local_path")
    if lp:
        name = _basename(lp)
        return f"{media_prefix}/{name}" if media_prefix else lp
    return row.get("origin_url") or ""


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def render_markdown(article: dict, blocks: list[dict], media_prefix: str | None = None) -> str:
    """把 article + blocks 渲染成 Markdown。

    media_prefix 由调用方决定：服务端传 `/media/{article_id}`，
    离线脚本传 `assets/{slug}`（默认 None 时直接用库里的相对路径）。
    """
    out: list[str] = [f"# {article.get('title') or 'Untitled'}"]
    head = [b for b in (article.get("sitename"), article.get("author"),
                        (article.get("published_at") or "")[:10]) if b]
    if head:
        out.append("> " + " · ".join(head))
    out.append(f"> 来源：{article.get('url')}")
    if article.get("word_count"):
        out.append(f"> 字数：{article['word_count']}")
    out.append("")

    aid = article.get("id")
    for b in blocks:
        t = b.get("type")
        text = (b.get("text") or "").strip()
        if t == "heading":
            lvl = min((b.get("level") or 2) + 1, 6)
            out.append(f"{'#' * lvl} {text}")
        elif t == "para":
            out.append(text)
        elif t == "quote":
            out.append(f"> {text}")
        elif t in ("list", "table"):
            out.append(text)
        elif t == "caption":
            out.append(f"*{text}*")
        elif t == "image":
            out.append(f"![{b.get('alt') or 'image'}]({_img_url(b, media_prefix)})")
        elif t == "video":
            out.append(f"[视频]({b.get('origin_url')})")
        if text or t in ("image", "video"):
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def domain_of(url: str) -> str:
    return urlparse(url).netloc


# ================================================================ 汇总清单

def _cell(text: object) -> str:
    """Markdown 表格单元格：竖线要转义，换行要压平，否则整张表会散架。"""
    s = "" if text is None else str(text)
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace("|", r"\|")


def _date_of(raw: object) -> str:
    s = "" if raw is None else str(raw)
    return s[:10] if len(s) >= 10 else s


def _meaning(main: object, ctx: object) -> str:
    """生词表里的「释义」= 词条本义 + 这句里的具体含义。

    只放语境义会得到一句像译文的说明（`她将莫高窟的记忆保存下来，传给后代。`），
    脱离这篇文章就没法用了；只放本义又丢了精读的语境。
    两个不同就都留着，本义在前。
    """
    a = "" if main is None else str(main).strip()
    b = "" if ctx is None else str(ctx).strip()
    if a and b and b != a:
        return f"{a}（此处：{b}）"
    return a or b


def render_summary_markdown(summary: dict) -> str:
    """单篇「生词表 + 语法清单」。格式与 DESIGN §6.5 一致。"""
    art = summary.get("article") or {}
    words = summary.get("words") or []
    grammar = summary.get("grammar") or []
    stats = summary.get("stats") or {}

    out: list[str] = [f"# {art.get('title') or art.get('url') or 'Untitled'}"]
    head = [b for b in (art.get("sitename"), art.get("author"), _date_of(art.get("published_at"))) if b]
    if head:
        out.append("> " + " · ".join(head))
    out.append(
        f"> 共 {stats.get('words', len(words))} 词"
        f" · {stats.get('grammar', len(grammar))} 语法点"
    )
    if art.get("url"):
        out.append(f"> 来源：{art['url']}")
    out.append("")

    out.append("## 生词表")
    out.append("")
    if not words:
        out.append("_（本篇还没有生词 —— 在正文里划几个词就会自动收录）_")
    else:
        out.append("| 单词 | 音标 | 词性 | 释义 | 文中例句 |")
        out.append("| --- | --- | --- | --- | --- |")
        for w in words:
            out.append(
                "| {lemma} | {ph} | {pos} | {mean} | {ex} |".format(
                    lemma=_cell(w.get("lemma") or w.get("surface")),
                    ph=_cell(w.get("phonetic") or "——"),
                    pos=_cell(w.get("pos")),
                    mean=_cell(_meaning(w.get("meaning_cn"), w.get("meaning_in_ctx"))),
                    ex=_cell(w.get("sentence") or w.get("example")),
                )
            )
    out.append("")

    out.append("## 语法清单")
    out.append("")
    if not grammar:
        out.append("_（本篇还没有语法点 —— 选中一个句子点「解析」就会收录）_")
    else:
        out.append("| 结构 | 类别 | 解析 | 原文例句 |")
        out.append("| --- | --- | --- | --- |")
        for g in grammar:
            out.append(
                "| {p} | {c} | {e} | {s} |".format(
                    p=_cell(g.get("pattern")),
                    c=_cell(g.get("category")),
                    e=_cell(g.get("explanation_cn")),
                    s=_cell(g.get("source_sentence")),
                )
            )
    out.append("")
    return "\n".join(out).rstrip() + "\n"


# ================================================================ Anki

# Anki 2.1.55+ 支持文件头指令。有了 #columns 就不该再写一行表头 ——
# 否则 Anki 会把表头当成第一张卡片导进去。
ANKI_HEADER = "#separator:Comma\n#html:true\n#columns:front,back,tags\n"

_TAG_BAD = re.compile(r"[\s\u3000]+")


def _anki_tag(*parts: object) -> str:
    out = []
    for p in parts:
        s = _TAG_BAD.sub("-", str(p or "").strip())
        if s:
            out.append(s)
    return " ".join(out)


def render_anki_csv(rows: list[dict], *, tag_suffix: str = "") -> str:
    """生词 → Anki 导入用 CSV（`front,back,tags`）。

    front 只放词形：先回忆读音和意思，翻过来才看音标与释义 —— 音标放正面等于
    直接把答案给出去。背面用 `<br>` 换行（配合 `#html:true`）。
    """
    buf = io.StringIO()
    buf.write(ANKI_HEADER)
    writer = csv.writer(buf, lineterminator="\n")
    for r in rows:
        front = (r.get("lemma") or r.get("surface") or "").strip()
        if not front:
            continue

        seg: list[str] = []
        head = " ".join(
            x for x in (
                f"<b>{escape(r['phonetic'])}</b>" if r.get("phonetic") else "",
                escape((r.get("pos") or "").strip()),
            ) if x
        )
        if head:
            seg.append(head)
        mean = _meaning(r.get("meaning_cn"), r.get("meaning_ctx"))
        if mean:
            seg.append(escape(mean))
        if r.get("sentence"):
            seg.append(f"<i>{escape(r['sentence'])}</i>")
        elif r.get("example"):
            seg.append(f"<i>{escape(r['example'])}</i>")
        if r.get("note"):
            seg.append(escape(r["note"]))
        if r.get("seen_count") and int(r["seen_count"]) > 1:
            seg.append(f"（划过 {int(r['seen_count'])} 次）")

        tags = _anki_tag("english", "精读", tag_suffix)
        writer.writerow([front, "<br>".join(seg), tags])
    return buf.getvalue()


def render_vocab_markdown(rows: list[dict], title: str = "生词本") -> str:
    """生词本的 Markdown（全局导出用，结构同单篇的生词表）。"""
    out: list[str] = [f"# {title}", "", f"> 共 {len(rows)} 词", ""]
    if not rows:
        out.append("_（还没有生词）_")
        return "\n".join(out) + "\n"
    out.append("| 单词 | 音标 | 词性 | 释义 | 出处 |")
    out.append("| --- | --- | --- | --- | --- |")
    for r in rows:
        out.append(
            "| {w} | {ph} | {pos} | {mean} | {src} |".format(
                w=_cell(r.get("lemma") or r.get("surface")),
                ph=_cell(r.get("phonetic") or "——"),
                pos=_cell(r.get("pos")),
                mean=_cell(_meaning(r.get("meaning_cn"), r.get("meaning_ctx"))),
                src=_cell(r.get("article_title") or "——"),
            )
        )
    out.append("")
    return "\n".join(out)
