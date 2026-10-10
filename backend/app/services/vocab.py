"""生词本（M4）。

## 入册规则：划过的词全收

DESIGN §11 把这一条留成了待定项（「划过的词全收，还是只收模型判定值得记的」）。
M4 的选择是**全收**，一直没变：

- 需求 5 的立场本来就是「协助过的内容全部记录」，生词本是这个立场最直接的产物；
- 漏收的代价是用户回头找不到那个词，多收一条的代价只是列表长一点 —— 不对等；
- `worth`（值不值得记）**不当作过滤器**，只用来排序与筛选（`?worth=1`），
  它判错的时候用户还能自己找回来。M10 起这个判定改由本地规则推导
  （考纲标签 + 词频，见 `services/ecdict.py::_worth`），不再是模型给的。

## 一词一行，出现记录分表

`vocab` 按 `lemma` 唯一，音标与释义跨文章复用（不在每篇里重查一遍）。
「在哪篇、哪句里划过」在 `vocab_sources`，一篇一词一条 ——
这才是「按文章筛生词」和「单篇汇总清单」的依据。

## 音标只从词典来，且必须是「原形那份」

`enroll` 的入参是本地词典查出来的词条（ECDICT 释义 + ipa-dict 音标，红线 3），
这里原样落库，不做二次加工 —— 但**哪一份词条**由调用方定：

    api/dictionary.py 传进来的是 `dictionary.lemma_entry(lookup(word))`，
    即 **lemma 自己**那份（划 `Experts` → 用它查到的 `expert` 条目）。

理由是这一行按 `lemma` 唯一、词头就是 `lemma`（见上一节）：音标与释义必须与
词头同源。M11 之前传的是**划到的词形**那份（词卡口径），于是 69 条里有 25 条
长成「expert /ˈɛkspɜːts/、释义（expert的复数形式）」。划到的形态仍然记在
`surface` 与 `vocab_sources.surface` 里，一个字都没少。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..db import now_iso, read, tx
from . import audio, dictionary

log = logging.getLogger("english.vocab")

STATUSES = ("new", "learning", "known")

# 列表页要的字段。article_count / article_titles 是子查询拼的（见 _list_sql）。
#
# srs_* 是 M5 的复习排期。**故意一起给**：生词本页与复习页是同一批词的两种看法，
# 前端不该为了画一个「复习中 · 间隔 12 天」的角标再发一轮请求。
SELECT_COLS = (
    "v.id, v.article_id, v.highlight_id, v.lemma, v.surface,"
    " v.phonetic_uk, v.phonetic_us, v.phonetic_source, v.phonetic_of,"
    " v.pos, v.meaning_cn, v.meaning_en, v.meaning_in_ctx, v.example,"
    " v.note, v.status, v.worth, v.seen_count, v.srs_due,"
    " v.srs_state, v.srs_step, v.srs_interval, v.srs_ease, v.srs_reps,"
    " v.srs_lapses, v.srs_last_at, v.srs_suspended,"
    " v.created_at, v.updated_at"
)

# 列表卡片要展示「当时划到的那句」。第一个 `?` 是「优先哪篇文章」：
# 按文章筛时优先给那篇里的句子，否则给最早出现的那句 —— 一律回最早那篇
# 会让人以为是别的文章里的词。`IS NOT` 能正确处理参数为 NULL 的情况。
#
# 注意这个 `?` 出现在 SELECT 列表里，位置**在 WHERE 之前**，所以调用方
# 拼参数时它必须排在 where 的条件参数前面（见 list_vocab / get_vocab）。
#
# 句子的**出处**（哪篇文章）不在这里算：那需要第二个同样的 `?`，
# 而多一个匿名占位符就意味着调用方得把同一个值传两遍，是个很容易踩的坑。
# 改由 `_sentence_sources()` 批量补一次 —— 见它的说明。
_SOURCES_SUB = (
    " (SELECT COUNT(DISTINCT s.article_id) FROM vocab_sources s"
    "   WHERE s.vocab_id = v.id) AS article_count,"
    " (SELECT GROUP_CONCAT(DISTINCT a2.title) FROM vocab_sources s2"
    "   LEFT JOIN articles a2 ON a2.id = s2.article_id"
    "   WHERE s2.vocab_id = v.id) AS article_titles,"
    " (SELECT s.sentence FROM vocab_sources s WHERE s.vocab_id = v.id"
    "   ORDER BY (s.article_id IS NOT ?), s.id ASC LIMIT 1) AS sentence"
    # 发音槽**不在这里**拼子查询：M10 起一个词有英美两行，子查询拼不出
    # 「按口音索引的字典」，改成 _attach_audios() 批量装（见那里）。
)

_ORDERS = {
    "recent": "v.id DESC",
    "alpha": "LOWER(v.lemma) ASC",
    # 值得记的排前面，同档次里划过次数多的排前面 —— 划过多次说明反复不认识
    "priority": "v.worth DESC, v.seen_count DESC, v.id DESC",
    "seen": "v.seen_count DESC, v.id DESC",
}


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # 音标展示口径统一走 dictionary.primary_phonetic（美音优先）
    d["phonetic"] = dictionary.primary_phonetic(
        d.get("phonetic_uk"), d.get("phonetic_us")
    )
    d["worth"] = int(d.get("worth") or 0)
    d["seen_count"] = int(d.get("seen_count") or 1)
    # SQLite 没有布尔类型，`srs_suspended` 取出来是 0/1；给前端一个真布尔，
    # 免得 `if (v.srs_suspended)` 这种写法在 TS 里被当成 number 用错
    d["srs_suspended"] = bool(d.get("srs_suspended"))
    # 发音槽：列表路径已经批量装好了（_attach_audios），单条路径在这里兜一下。
    # 兜底走 audio.slots() 会开两次短连接 —— 单条无所谓，别在循环里调它。
    if "audios" not in d:
        d["audios"] = audio.slots(dictionary.normalize_word(str(d.get("lemma") or "")))
    return d


def _attach_audios(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """给整页生词批量装上英美两个发音槽（M10）。

    为什么不在 `_SOURCES_SUB` 里拼子查询：一个词有**两行**（uk / us），
    子查询拼不出「按口音索引的字典」，硬拼就是两个词各来三条子查询 —— 列会炸。
    改成一次 `IN (...)` 取回整页的槽位、Python 侧按词分组，
    一屏 100 条也只多一次查询（与 `_sentence_sources` 同一个理由）。

    槽位结构复用 `audio.slot()`：URL 拼法、`missing` 不画喇叭这些口径
    在词卡与生词本之间必须是**同一份实现**，否则迟早漂移。
    """
    if not rows:
        return
    keys = [dictionary.normalize_word(str(r.get("lemma") or "")) for r in rows]
    marks = ",".join("?" * len(keys))
    found: dict[str, dict[str, sqlite3.Row]] = {}
    for r in conn.execute(
        # 列要照 `audio.slot()` 的需要给全 —— 少给一列（`path`）会在那边
        # 取行字段时直接抛 IndexError，而它只在「本地已有文件」那条分支上用到，
        # 不点发音就看不出来（实测踩过：列表页 500，词卡却好着）。
        "SELECT word, accent, status, origin, got_accent, path FROM audio_cache"
        f" WHERE word IN ({marks})",
        keys,
    ):
        found.setdefault(str(r["word"]), {})[str(r["accent"])] = r
    for row, key in zip(rows, keys):
        slots = found.get(key, {})
        row["audios"] = {
            accent: audio.slot(key, slots.get(accent), accent) for accent in audio.ACCENTS
        }


def normalize_lemma(raw: str) -> str:
    """入册用的键。

    **不转小写** —— `US`（美国）和 `us`（我们）是两个词，压成同一个键
    会把两条互不相干的记录并在一起。这里只去空白。
    词典给的 lemma 本来就是原形，大小写是稳定的。
    """
    return " ".join((raw or "").split()).strip(".,;:!?\"'()[]")


def enroll(
    conn: sqlite3.Connection,
    *,
    article_id: int,
    highlight_id: int | None,
    word: str,
    result: dict[str, Any],
    sentence: str = "",
) -> dict[str, Any]:
    """把一次成功的 dict 调用登记进生词本（幂等）。

    `word` 是用户**划到**的形态（`Experiments`），`result` 是**原形那份**词条
    （`experiment`，调用方用 `dictionary.lemma_entry` 换过）：前者进 `surface`
    与 `vocab_sources.surface`（「在哪儿划过什么形态」），后者填这一行的
    词头/音标/释义。两者混用就会出现词头与音标对不上的行 —— 见模块 docstring。

    必须在调用方的写事务里执行 —— 它要和 `ai_logs` 那条流水同生共死：
    日志写进去了而词没入册（或反过来）都会让「痕迹」出现缺口。
    """
    lemma = normalize_lemma(result.get("lemma") or word)
    if not lemma:
        return {"enrolled": False, "reason": "empty_lemma"}

    surface = (word or lemma).strip() or lemma
    now = now_iso()

    # 音标**词典给几条就是几条**：只有一侧就只存一侧。
    #
    # 以前这里写成 `ph_uk = ... or result["phonetic"]`，本意是「单侧音标也别丢」，
    # 实际效果是把那一侧的读音**复制到另一侧** —— `Tuesday` / `pension` /
    # `arithmetic` 这几条因此在库里凭空多出一条英音（ipa-dict 的英音表里
    # 根本没有它们）。那是编造，红线 3 的立场是「缺失就不显示」。
    # M12 起生词本卡会把英美两条并排显示，这种假数据会当场露馅。
    #
    # 两侧都空时才用通用 `phonetic` 兜底（老形状的入参才有这个字段），按英式
    # 落 uk —— 与 `dictionary._phonetics` 第 3 条（ECDICT 单条音标）同一口径。
    ph_uk = result.get("phonetic_uk")
    ph_us = result.get("phonetic_us")
    if not ph_uk and not ph_us:
        ph_uk = result.get("phonetic")
    worth = 1 if result.get("worth", 1) else 0

    row = conn.execute("SELECT id, status, worth FROM vocab WHERE lemma = ?", (lemma,)).fetchone()
    if row is None:
        # srs_due 直接给「现在」：新词入册即到期，复习队列才会认它（M5）。
        # 队列对「从没复习过」的词另有数量上限，所以这里不会把队列灌爆。
        cur = conn.execute(
            "INSERT INTO vocab (article_id, highlight_id, lemma, surface,"
            " phonetic_uk, phonetic_us, phonetic_source, phonetic_of, pos,"
            " meaning_cn, meaning_en, meaning_in_ctx, example, status, worth,"
            " seen_count, srs_due, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (
                article_id, highlight_id, lemma, surface,
                ph_uk, ph_us, result.get("phonetic_source"), result.get("phonetic_of"),
                result.get("pos"),
                result.get("meaning_cn"), result.get("meaning_en"),
                result.get("meaning_in_ctx"), result.get("example"),
                "new", worth, now, now, now,
            ),
        )
        vocab_id = int(cur.lastrowid)
        created = True
    else:
        vocab_id = int(row["id"])
        created = False
        # 「这一次算不算又划到了它」决定 seen_count 要不要 +1。
        #
        # 为什么不能无脑 +1：M10 起点正文里已划过的单词**就能重弹词卡**，
        # 反复点开同一个词会把 seen_count 刷成十几次，而它喂的是
        # `sort=seen`（「划过多次说明反复不认识」）与列表上的角标 ——
        # 数字一虚，那个排序就失去意义。
        #
        # 判据是 vocab_sources 里那条「同篇同形态」的出现记录在不在：
        # 在 = 同一篇文章里同一个词又查了一次，不算新遇见。
        is_new_sighting = conn.execute(
            "SELECT 1 FROM vocab_sources WHERE vocab_id = ? AND article_id = ?"
            " AND surface = ?",
            (vocab_id, article_id, surface),
        ).fetchone() is None
        # 两套口径，故意不一样：
        #
        #   音标系列 —— 用户改不了（只可能来自词典），所以**整个替换**，不用
        #     COALESCE。这一行的词头是 lemma，重划一次拿到的就是同一份原形词条，
        #     直接赋值天然幂等；而 COALESCE 有两处坏处：第一次查到的坏值会被
        #     永久钉住（M4 实测：preserved 挂上了过去式的读音），以及词典后来
        #     确认「没有这一侧」时（值为 NULL）旧值清不掉。
        #   释义/批注 —— 用户能改，所以**旧值优先**，只补空。
        conn.execute(
            "UPDATE vocab SET"
            "  seen_count = seen_count + ?,"
            "  worth = MAX(worth, ?),"
            "  phonetic_uk = ?,"
            "  phonetic_us = ?,"
            "  phonetic_source = ?,"
            "  phonetic_of = ?,"
            "  pos = COALESCE(pos, ?),"
            "  meaning_cn = COALESCE(meaning_cn, ?),"
            "  meaning_en = COALESCE(meaning_en, ?),"
            "  meaning_in_ctx = COALESCE(meaning_in_ctx, ?),"
            "  example = COALESCE(example, ?),"
            "  updated_at = ?"
            " WHERE id = ?",
            (
                1 if is_new_sighting else 0,
                worth, ph_uk, ph_us, result.get("phonetic_source"),
                result.get("phonetic_of"), result.get("pos"),
                result.get("meaning_cn"), result.get("meaning_en"),
                result.get("meaning_in_ctx"), result.get("example"),
                now, vocab_id,
            ),
        )

    # 出现记录：同篇同形态只留一条，重复划到就把上下文更新成最新的
    conn.execute(
        "INSERT INTO vocab_sources (vocab_id, article_id, highlight_id, surface,"
        " sentence, meaning_in_ctx, created_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(vocab_id, article_id, surface) DO UPDATE SET"
        "   sentence = COALESCE(excluded.sentence, sentence),"
        "   meaning_in_ctx = COALESCE(excluded.meaning_in_ctx, meaning_in_ctx),"
        "   highlight_id = COALESCE(excluded.highlight_id, highlight_id)",
        (
            vocab_id, article_id, highlight_id, surface,
            (sentence or "").strip()[:2000] or None,
            (result.get("meaning_in_ctx") or "").strip()[:500] or None,
            now,
        ),
    )

    _fts_sync(conn, vocab_id)
    return {"enrolled": True, "vocab_id": vocab_id, "lemma": lemma, "created": created}


# ---------------------------------------------------------------- 查询


def _list_sql(where: str) -> str:
    return f"SELECT {SELECT_COLS},{_SOURCES_SUB} FROM vocab v {where}"


def _sentence_sources(
    conn: sqlite3.Connection, rows: list[dict], prefer_article_id: int | None
) -> None:
    """给每条补上「卡片上那句例句出自哪篇」。

    `vocab.article_id` 是**首次入册那篇**，而卡片上的句子是按
    「优先当前筛的那篇、否则最早那篇」挑的 —— 两者对同一个词常常不是一篇
    （词在第二篇文章里再被划过时就会分叉）。拿 `article_id` 当出处的链接会
    指到一篇压根没有这句话的文章里。所以出处必须单独查，且用**同一套排序**。

    批量查而不是逐行子查询：`IN (...)` 一次拿回所有候选，Python 侧每组取第一条。
    列表页一屏 100 条也不会变成 100 次查询。
    """
    if not rows:
        return
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    found: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT s.vocab_id, s.article_id, s.id, a.title AS article_title"
        " FROM vocab_sources s LEFT JOIN articles a ON a.id = s.article_id"
        f" WHERE s.vocab_id IN ({marks})"
        " ORDER BY (s.article_id IS NOT ?), s.id ASC",
        [*ids, prefer_article_id],
    ):
        found.setdefault(int(r["vocab_id"]), r)
    for row in rows:
        hit = found.get(int(row["id"]))
        row["sentence_article_id"] = hit["article_id"] if hit else None
        row["sentence_article_title"] = hit["article_title"] if hit else None


def list_vocab(
    *,
    article_id: int | None = None,
    status: str | None = None,
    worth: int | None = None,
    q: str | None = None,
    sort: str = "recent",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where, params = [], []
    if article_id is not None:
        where.append("EXISTS (SELECT 1 FROM vocab_sources s WHERE s.vocab_id = v.id AND s.article_id = ?)")
        params.append(article_id)
    if status:
        where.append("v.status = ?")
        params.append(status)
    if worth is not None:
        where.append("v.worth = ?")
        params.append(1 if worth else 0)
    if q:
        # 列表页的搜索框要的是「输入片段就能找到」，LIKE 比 FTS 更合手
        where.append("(v.lemma LIKE ? OR v.meaning_cn LIKE ? OR v.surface LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = _ORDERS.get(sort, _ORDERS["recent"])

    # SELECT 列表里的那个 ? 排在最前（见 _SOURCES_SUB 的说明）
    lead = [article_id]

    with read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM vocab v {clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"{_list_sql(clause)} ORDER BY {order} LIMIT ? OFFSET ?",
            [*lead, *params, limit, offset],
        ).fetchall()
        # 先转 dict 再装发音槽：_row() 看到 audios 已在就不再去逐条查（见 _attach_audios）
        raw = [dict(r) for r in rows]
        _attach_audios(conn, raw)
        items = [_row(r) for r in raw]
        _sentence_sources(conn, items, article_id)
    return {"total": total, "items": items}


def get_vocab(vocab_id: int) -> dict | None:
    with read() as conn:
        row = conn.execute(
            f"{_list_sql('WHERE v.id = ?')}", [None, vocab_id]
        ).fetchone()
        if row is None:
            return None
        raw = dict(row)
        _attach_audios(conn, [raw])
        d = _row(raw)
        if d is None:
            return None
        _sentence_sources(conn, [d], None)
        d["sources"] = [
            dict(r)
            for r in conn.execute(
                "SELECT s.id, s.article_id, s.highlight_id, s.surface, s.sentence,"
                " s.meaning_in_ctx, s.created_at, a.title AS article_title"
                " FROM vocab_sources s LEFT JOIN articles a ON a.id = s.article_id"
                " WHERE s.vocab_id = ? ORDER BY s.id DESC",
                (vocab_id,),
            ).fetchall()
        ]
    return d


def update_vocab(
    vocab_id: int,
    *,
    status: str | None = None,
    note: str | None = None,
    meaning_cn: str | None = None,
    worth: int | None = None,
) -> dict | None:
    sets, params = [], []
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"未知的状态：{status}")
        sets.append("status = ?")
        params.append(status)
    if note is not None:
        sets.append("note = ?")
        params.append(note.strip()[:2000] or None)
    if meaning_cn is not None:
        sets.append("meaning_cn = ?")
        params.append(meaning_cn.strip()[:500] or None)
    if worth is not None:
        sets.append("worth = ?")
        params.append(1 if worth else 0)
    if not sets:
        return get_vocab(vocab_id)

    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(vocab_id)

    with tx() as conn:
        cur = conn.execute(f"UPDATE vocab SET {', '.join(sets)} WHERE id = ?", params)
        if cur.rowcount == 0:
            return None
        # 释义改了要同步 FTS，否则检索结果还指向旧意思
        _fts_sync(conn, vocab_id)
    return get_vocab(vocab_id)


def delete_vocab(vocab_id: int) -> bool:
    with tx() as conn:
        row = conn.execute("SELECT id FROM vocab WHERE id = ?", (vocab_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM fts_vocab WHERE vocab_id = ?", (vocab_id,))
        conn.execute("DELETE FROM vocab WHERE id = ?", (vocab_id,))
    return True


def stats() -> dict:
    with read() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM vocab").fetchone()["c"]
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) n FROM vocab GROUP BY status"
            )
        }
        worth = conn.execute("SELECT COUNT(*) c FROM vocab WHERE worth = 1").fetchone()["c"]
        today = conn.execute(
            "SELECT COUNT(*) c FROM vocab WHERE substr(created_at, 1, 10) = substr(?, 1, 10)",
            (now_iso(),),
        ).fetchone()["c"]
    return {
        "total": total,
        "worth": worth,
        "today": today,
        "by_status": {s: by_status.get(s, 0) for s in STATUSES},
    }


def _fts_sync(conn: sqlite3.Connection, vocab_id: int) -> None:
    """FTS5 是独立表，得自己维护 —— 没有触发器帮忙。"""
    conn.execute("DELETE FROM fts_vocab WHERE vocab_id = ?", (vocab_id,))
    row = conn.execute(
        "SELECT lemma, meaning_cn, example FROM vocab WHERE id = ?", (vocab_id,)
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO fts_vocab (lemma, meaning_cn, example, vocab_id) VALUES (?,?,?,?)",
        (row["lemma"] or "", row["meaning_cn"] or "", row["example"] or "", vocab_id),
    )


def reindex_all() -> int:
    """重建 fts_vocab。脚本用（迁移后 / 手工改库后）。"""
    with tx() as conn:
        conn.execute("DELETE FROM fts_vocab")
        ids = [r["id"] for r in conn.execute("SELECT id FROM vocab")]
        for vid in ids:
            _fts_sync(conn, vid)
    return len(ids)
