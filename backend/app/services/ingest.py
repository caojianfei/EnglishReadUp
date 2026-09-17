"""存储层：把抓取结果写进 SQLite，并对外提供文章读写。

重抓不伤学习痕迹的机制（docs/DESIGN.md §4 红线 1）：
  blocks.block_uid = sha1(类型 + 内容指纹 + 同内容出现序号)
  重抓时按 uid 命中旧行 → 复用原 id 只更新内容与顺序；
  本次未出现的旧 uid → 标记 is_stale=1（绝不物理删除，删除会 CASCADE 掉 highlights）。
  渲染层只取 is_stale=0 的块，旧痕迹仍可凭 block_id 找到原文。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from ..config import settings
from ..db import now_iso, read, row_to_dict, tx
from .fetcher import Block, FetchResult, canonical_url, slug_of


# ---------------------------------------------------------------- 内部工具

def _block_uid(b: Block, counter: dict[str, int]) -> str:
    """内容指纹 + 同内容出现序号，保证同篇内唯一且重抓后稳定。"""
    if b.type == "image":
        key = f"image|{urlparse(b.origin_url or '').path}"
    else:
        norm = re.sub(r"\s+", " ", (b.text or "")).strip()[:300]
        key = f"{b.type}|{norm}"
    n = counter.get(key, 0)
    counter[key] = n + 1
    return hashlib.sha1(f"{key}#{n}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 标题块

# 标题在 blocks.type 里的取值，以及它占的序位。
# 正文块由 fetcher 重排成 1..n（见 drop_duplicate_title 末尾的 `b.order = j + 1`），
# 标题占 0，`ORDER BY order_index` 天然排在正文之前。
TITLE_TYPE = "title"
TITLE_ORDER = 0


def title_block(title: str | None) -> Block | None:
    """把文章标题也做成一个块。

    标题本来只存在 `articles.title` 里，于是它有一条正文没有的特权：划不了词。
    而划词的坐标系只有 `blocks.text` 一套（红线 2），要让标题能划、能落痕迹、
    重抓后坐标依然有效，它就必须是 blocks 里的一行 —— 而不是渲染层临时拼出来的标签。

    类型单独用 `title` 而不是复用 `heading`，因为两边的处置本来就不同：
    前端把它渲染成页面标题（不带正文小标题的样式与间距），导出侧 `# 标题` 已经
    由 `render_markdown` 开头写了一行，不必再出一行。
    """
    text = (title or "").strip()
    if not text:
        return None
    return Block(order=TITLE_ORDER, type=TITLE_TYPE, text=text, level=1)


def _upsert_image(conn, article_id: int, b: Block) -> int:
    row = conn.execute(
        "SELECT id FROM images WHERE article_id = ? AND origin_url = ?",
        (article_id, b.origin_url),
    ).fetchone()
    status = "ok" if b.local_path else "failed"
    if row:
        conn.execute(
            "UPDATE images SET local_path=?, alt=?, download_status=? WHERE id=?",
            (b.local_path, b.alt or None, status, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO images (article_id, origin_url, local_path, alt, download_status)"
        " VALUES (?,?,?,?,?)",
        (article_id, b.origin_url, b.local_path, b.alt or None, status),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 文章

def get_or_create_article(url: str) -> tuple[int, bool]:
    """按 canonical_url 查重。返回 (article_id, 新建与否)。"""
    canon = canonical_url(url)
    now = now_iso()
    with tx() as conn:
        row = conn.execute("SELECT id FROM articles WHERE canonical_url = ?", (canon,)).fetchone()
        if row:
            return int(row["id"]), False
        cur = conn.execute(
            "INSERT INTO articles (url, canonical_url, source_domain, status, created_at)"
            " VALUES (?,?,?,'pending',?)",
            (url.strip(), canon, urlparse(canon).netloc.replace("www.", ""), now),
        )
        return int(cur.lastrowid), True


def mark_status(article_id: int, status: str, error: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE articles SET status=?, error_msg=?, updated_at=? WHERE id=?",
            (status, (error or "")[:2000] or None, now_iso(), article_id),
        )


def save_fetch(article_id: int, res: FetchResult) -> None:
    """把抓取结果写入 articles / blocks / images，并重建该文章的 FTS 索引。

    写入的块 = **标题块**（见 `title_block`）+ 抓到的正文块。
    标题也走同一套「uid 命中就复用行 id」的机制，所以重抓不会丢掉标题上的痕迹。
    """
    rep = res.report
    now = now_iso()
    head = title_block(rep.title)
    blocks = ([head] if head else []) + list(res.blocks)

    with tx() as conn:
        prev = {
            r["block_uid"]: int(r["id"])
            for r in conn.execute(
                "SELECT id, block_uid FROM blocks WHERE article_id = ?", (article_id,)
            )
        }
        counter: dict[str, int] = {}
        seen: set[str] = set()
        keep_images: list[int] = []
        fts_rows: list[tuple[str, int, int]] = []
        cover_path: str | None = None

        for b in blocks:
            uid = _block_uid(b, counter)
            seen.add(uid)
            image_id = None
            text = None if b.type == "image" else (b.text or "")

            if b.type == "image" and b.origin_url:
                image_id = _upsert_image(conn, article_id, b)
                keep_images.append(image_id)
                if b.local_path and cover_path is None:
                    cover_path = b.local_path

            if uid in prev:
                bid = prev[uid]
                conn.execute(
                    "UPDATE blocks SET order_index=?, type=?, text=?, level=?, image_id=?, is_stale=0"
                    " WHERE id=?",
                    (b.order, b.type, text, b.level, image_id, bid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO blocks (article_id, block_uid, order_index, type, text, level,"
                    " image_id, is_stale) VALUES (?,?,?,?,?,?,?,0)",
                    (article_id, uid, b.order, b.type, text, b.level, image_id),
                )
                bid = int(cur.lastrowid)

            if text and text.strip():
                fts_rows.append((text[:20000], article_id, bid))

        # 本次未出现的旧块：标记 stale 而非删除
        for uid, bid in prev.items():
            if uid not in seen:
                conn.execute("UPDATE blocks SET is_stale=1, order_index=-1 WHERE id=?", (bid,))

        # 清理不再被引用的图片（先解开 blocks.image_id 引用再删）
        if keep_images:
            marks = ",".join("?" * len(keep_images))
            conn.execute(
                f"UPDATE blocks SET image_id=NULL WHERE article_id=? AND image_id IS NOT NULL"
                f" AND image_id NOT IN ({marks})",
                (article_id, *keep_images),
            )
            conn.execute(
                f"DELETE FROM images WHERE article_id=? AND id NOT IN ({marks})",
                (article_id, *keep_images),
            )
        else:
            conn.execute("UPDATE blocks SET image_id=NULL WHERE article_id=?", (article_id,))
            conn.execute("DELETE FROM images WHERE article_id=?", (article_id,))

        # FTS 重建
        conn.execute("DELETE FROM fts_blocks WHERE article_id = ?", (article_id,))
        if fts_rows:
            conn.executemany(
                "INSERT INTO fts_blocks (text, article_id, block_id) VALUES (?,?,?)", fts_rows
            )

        conn.execute(
            "UPDATE articles SET title=?, sitename=?, author=?, published_at=?, lang=?,"
            " word_count=?, cover_path=?, raw_html_path=?, extractor_pass=?, status=?,"
            " error_msg=?, fetched_at=?, updated_at=? WHERE id=?",
            (
                rep.title or None,
                rep.sitename or None,
                rep.author or None,
                rep.date or None,
                rep.language or "en",
                rep.words,
                cover_path,
                res.raw_html_path or None,
                rep.extractor_pass or None,
                "done" if res.ok else "failed",
                ("; ".join(rep.errors))[:2000] or None,
                now,
                now,
                article_id,
            ),
        )


# ---------------------------------------------------------------- 查询

ARTICLE_COLS = (
    "a.id, a.url, a.canonical_url, a.title, a.source_domain, a.sitename, a.author,"
    " a.published_at, a.lang, a.word_count, a.difficulty, a.cover_path, a.extractor_pass,"
    " a.status, a.error_msg, a.fetched_at, a.created_at,"
    " p.last_block_id, p.scroll_pct, COALESCE(p.read_seconds, 0) AS read_seconds,"
    " (SELECT COUNT(*) FROM blocks b WHERE b.article_id = a.id AND b.is_stale = 0)"
    "   AS block_count,"
    " (SELECT COUNT(*) FROM images i WHERE i.article_id = a.id AND i.local_path IS NOT NULL)"
    "   AS image_count"
)

# 详情与列表共用这个 join：列表页要显示封面/进度，详情页要进度。
# 计数也一并由子查询带出 —— 否则列表页的 block_count 会永远显示 0。
ARTICLE_FROM = "FROM articles a LEFT JOIN article_progress p ON p.article_id = a.id"


def _media_url(article_id: int, local_path: str | None) -> str | None:
    if not local_path:
        return None
    return f"/media/{article_id}/{re.split(r'[\\\\/]', local_path)[-1]}"


def get_article(article_id: int) -> dict | None:
    with read() as conn:
        row = conn.execute(
            f"SELECT {ARTICLE_COLS} {ARTICLE_FROM} WHERE a.id = ?", (article_id,)
        ).fetchone()
    if row is None:
        return None
    art = dict(row)
    art["cover_url"] = _media_url(article_id, art.get("cover_path"))
    return art


def get_article_by_canonical(url: str) -> dict | None:
    canon = canonical_url(url)
    with read() as conn:
        row = conn.execute("SELECT id FROM articles WHERE canonical_url = ?", (canon,)).fetchone()
    return get_article(int(row["id"])) if row else None


def list_articles(limit: int = 50, offset: int = 0, q: str | None = None) -> dict:
    where, params = "", []
    if q:
        where = "WHERE a.title LIKE ? OR a.url LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    with read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM articles a {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {ARTICLE_COLS} {ARTICLE_FROM} {where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        # 列表页要显示封面缩略图，封面地址同样在这里补
        d["cover_url"] = _media_url(d["id"], d.get("cover_path"))
        items.append(d)
    return {"total": total, "items": items}


def get_blocks(article_id: int, include_stale: bool = False) -> list[dict]:
    """取文章块，图片信息平铺进块，供前端直接渲染。"""
    sql = (
        "SELECT b.id, b.order_index, b.type, b.text, b.level, b.is_stale,"
        " i.id AS image_id, i.origin_url, i.local_path, i.alt, i.download_status"
        " FROM blocks b LEFT JOIN images i ON i.id = b.image_id"
        " WHERE b.article_id = ?"
    )
    if not include_stale:
        sql += " AND b.is_stale = 0"
    sql += " ORDER BY b.order_index, b.id"
    with read() as conn:
        rows = [dict(r) for r in conn.execute(sql, (article_id,)).fetchall()]
    for r in rows:
        r["media_url"] = _media_url(article_id, r.get("local_path"))
    return rows


# ---------------------------------------------------------------- 阅读进度

PROGRESS_COLS = "article_id, last_block_id, scroll_pct, read_seconds, updated_at"


def get_progress(article_id: int) -> dict | None:
    with read() as conn:
        row = conn.execute(
            f"SELECT {PROGRESS_COLS} FROM article_progress WHERE article_id = ?", (article_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def save_progress(
    article_id: int,
    last_block_id: int | None = None,
    scroll_pct: float | None = None,
    add_seconds: int = 0,
) -> dict:
    """写入/累加阅读进度。

    - `add_seconds` 是增量，直接累加到已有总时长上（重复上报也不会冲掉历史）。
    - `last_block_id` 会校验归属：传入不属于该文章的块 id 时按 None 处理，
      避免脏数据把「继续阅读」按钮指到别人的文章上。
    - 进度表与原文/blobks 分离，重抓文章不影响阅读位置（红线 1）。
    """
    now = now_iso()
    with tx() as conn:
        bid = last_block_id
        if bid is not None:
            ok = conn.execute(
                "SELECT 1 FROM blocks WHERE id = ? AND article_id = ?", (bid, article_id)
            ).fetchone()
            bid = int(bid) if ok else None
        conn.execute(
            "INSERT INTO article_progress"
            " (article_id, last_block_id, scroll_pct, read_seconds, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(article_id) DO UPDATE SET"
            "   last_block_id = COALESCE(excluded.last_block_id, article_progress.last_block_id),"
            "   scroll_pct    = COALESCE(excluded.scroll_pct, article_progress.scroll_pct),"
            "   read_seconds  = article_progress.read_seconds + excluded.read_seconds,"
            "   updated_at    = excluded.updated_at",
            (article_id, bid, scroll_pct, max(0, int(add_seconds)), now),
        )
        row = conn.execute(
            f"SELECT {PROGRESS_COLS} FROM article_progress WHERE article_id = ?", (article_id,)
        ).fetchone()
    return dict(row) if row is not None else {
        "article_id": article_id,
        "last_block_id": bid,
        "scroll_pct": scroll_pct,
        "read_seconds": max(0, int(add_seconds)),
        "updated_at": now,
    }


def delete_article(article_id: int) -> bool:
    art = get_article(article_id)
    if art is None:
        return False
    with tx() as conn:
        conn.execute("DELETE FROM fts_blocks WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    # 文件系统清理（raw + assets）
    for p in (settings.raw_dir / f"{article_id}.html",):
        p.unlink(missing_ok=True)
    assets = settings.assets_dir / str(article_id)
    if assets.exists():
        for f in assets.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
        assets.rmdir()
    return True


def search_blocks(q: str, limit: int = 30) -> list[dict]:
    """FTS5 全文检索正文。查询串里的双引号会破坏语法，先转义为短语。"""
    phrase = '"' + q.replace('"', " ").strip() + '"'
    with read() as conn:
        rows = conn.execute(
            "SELECT f.block_id, f.article_id, a.title, a.url,"
            " snippet(fts_blocks, 0, '<mark>', '</mark>', '…', 12) AS snip"
            " FROM fts_blocks f JOIN articles a ON a.id = f.article_id"
            " WHERE fts_blocks MATCH ? LIMIT ?",
            (phrase, limit),
        ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "Block",
    "FetchResult",
    "TITLE_ORDER",
    "TITLE_TYPE",
    "canonical_url",
    "delete_article",
    "get_article",
    "get_article_by_canonical",
    "get_blocks",
    "get_or_create_article",
    "get_progress",
    "list_articles",
    "mark_status",
    "row_to_dict",
    "save_fetch",
    "save_progress",
    "search_blocks",
    "slug_of",
    "title_block",
]
