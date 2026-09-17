"""间隔复习（M5）。

## 算法：SM-2 的简化变体

四档评分（Anki 的语义，学习者的直觉也是这四个）：

    again   完全没想起来   → 回到学习阶段的第一步，review 阶段算一次忘 (lapse)
    hard    想起来了但很费劲 → 间隔按 1.2 倍长，难度因子 −0.15
    good    正常想起来     → 间隔 × 难度因子
    easy    太简单         → 间隔 × 难度因子 × 1.3，难度因子 +0.15

比教科书 SM-2（0~5 六档）少了中间档：手机上点六档没人愿意选，而四档的
「错误率 / 正确率」统计反而更干净。

学习阶段（`srs_state='learning'`）用分钟级的两步 1 → 10 分钟，走完就毕业进
review，起手 1 天。`easy` 可以跳过剩下的步直接毕业，起手 4 天。这跟 Anki 的
learning steps 是一套东西。

## 为什么 `status` 不跟着算法走

`vocab.status`（new/learning/known）是 **M4 留给用户的主观标记**，复习进度写在
`srs_state`。两套并行看起来有点重复，但合并是错的：同一列被算法和用户同时写，
必然互相覆盖 —— 用户刚标「已掌握」，下一次评分就把它改回「在学」，
用户会觉得这个按钮是假的。算法进度本来就是客观事实，不需要用户再维护一遍。

## 新词为什么不无限灌进队列

划词即入册，而生词本是「划过的词全收」（M4 的口径），所以一天涨几十个很正常，
里面还会混着 this / make / get。队列因此对**从没复习过**的词单独限量
（`new_limit`，默认 20），到期的旧词不设限 —— 旧词是「欠的债」，新词是「可选的」。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import now_iso, read, tx
from . import vocab

log = logging.getLogger("english.srs")

GRADES = ("again", "hard", "good", "easy")
GRADE_LABEL = {"again": "忘了", "hard": "勉强", "good": "记得", "easy": "太简单"}

# 学习阶段的两步（分钟）。1 / 10 是 Anki 的默认值，够用就不改。
LEARN_STEPS_MIN: tuple[int, ...] = (1, 10)

EASE_START = 2.5
EASE_MIN = 1.3
EASE_MAX = 3.2

# 间隔上限：一年。不设的话连点几次 easy 就能把间隔推到几十年，
# 数值荒唐，也让「下次到期」这个提示失去意义。
MAX_INTERVAL_DAYS = 365.0

DEFAULT_NEW_LIMIT = 20
DEFAULT_LIMIT = 60


# ---------------------------------------------------------------- 时间

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _due(now: datetime, *, minutes: float = 0, days: float = 0) -> str:
    return _iso(now + timedelta(minutes=minutes, days=days))


def human_interval(due: str | None, now: datetime | None = None) -> str:
    """把「下次到期」说成人话。按钮上要显示的就是它。

    刻意从 `due` 反推而不是直接用 interval 字段：学习阶段的 interval 是 0，
    真正的等待在分钟上，两条分支用同一个函数才不会有谁漏了单位。
    """
    t = _parse(due)
    if t is None:
        return "—"
    now = now or _now()
    minutes = (t - now).total_seconds() / 60
    if minutes < 1:
        return "现在"
    if minutes < 60:
        return f"{round(minutes)} 分钟"
    hours = minutes / 60
    if hours < 23:
        return f"{round(hours)} 小时"
    days = hours / 24
    if days < 1.5:
        return "1 天"
    if days < 30:
        return f"{round(days)} 天"
    if days < 365:
        return f"{days / 30:.1f} 个月"
    return f"{days / 365:.1f} 年"


# ---------------------------------------------------------------- 调度

def _clamp_ease(v: float) -> float:
    return round(min(EASE_MAX, max(EASE_MIN, v)), 3)


def _result(
    state: str, step: int, interval: float, ease: float, reps: int, lapses: int,
    was_lapse: int, due: str,
) -> dict[str, Any]:
    return {
        "state": state,
        "step": step,
        "interval": round(interval, 2),
        "ease": ease,
        "reps": reps,
        "lapses": lapses,
        "was_lapse": was_lapse,
        "due": due,
    }


def schedule(
    *,
    state: str | None,
    step: int | None,
    interval: float | None,
    ease: float | None,
    reps: int | None,
    lapses: int | None,
    grade: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """纯函数：给一张卡的当前排期 + 一次评分，算出新排期。

    不读库、不写库 —— 队列里每个按钮上的「下次 3 天」也是靠它算出来的，
    和真正评分走同一条路径，所以按下去的间隔与按钮上写的一定一致。
    """
    if grade not in GRADES:
        raise ValueError(f"未知的评分：{grade}（可选 {'/'.join(GRADES)}）")

    now = now or _now()
    state = state or "new"
    step = int(step or 0)
    interval = float(interval or 0.0)
    ease = _clamp_ease(float(ease or EASE_START))
    reps = int(reps or 0)
    lapses = int(lapses or 0)

    # ---- 已毕业：按天推进 ----
    if state == "review":
        if grade == "again":
            # 「间隔折半」而不是直接归 1 天：攒了半年的进度一次清零，
            # 惩罚过重会让人宁愿卡着也不点「忘了」，那这个档位就废了。
            interval = max(1.0, round(interval * 0.5, 2)) if interval else 1.0
            return _result(
                "learning", 0, interval, _clamp_ease(ease - 0.20),
                reps, lapses + 1, 1, _due(now, minutes=LEARN_STEPS_MIN[0]),
            )
        if grade == "hard":
            ease = _clamp_ease(ease - 0.15)
            interval = min(MAX_INTERVAL_DAYS, max(1.0, interval * 1.2))
        elif grade == "good":
            interval = min(MAX_INTERVAL_DAYS, max(1.0, interval * ease))
        else:  # easy
            ease = _clamp_ease(ease + 0.15)
            interval = min(MAX_INTERVAL_DAYS, max(1.0, interval * ease * 1.3))
        return _result(
            "review", step, interval, ease, reps + 1, lapses, 0, _due(now, days=interval)
        )

    # ---- 新词 / 学习中：按分钟推进，走完步数就毕业 ----
    #
    # 这一段刻意**不动 ease**：难度因子是「长期有多难」的度量，
    # 新词阶段只看了两三次就下结论太早，而且学习阶段怎么走都由步数决定，
    # 改 ease 也影响不到当前这条路径，只会让毕业后的第一个间隔莫名其妙。
    n = len(LEARN_STEPS_MIN)
    if grade == "again":
        return _result(
            "learning", 0, 0.0, ease, reps, lapses, 0,
            _due(now, minutes=LEARN_STEPS_MIN[0]),
        )
    if grade == "hard":
        # 不进不退：留在当前这一步再走一遍
        return _result(
            "learning", step, 0.0, ease, reps, lapses, 0,
            _due(now, minutes=LEARN_STEPS_MIN[min(step, n - 1)]),
        )
    if grade == "good":
        nxt = step + 1
        if nxt < n:
            return _result(
                "learning", nxt, 0.0, ease, reps, lapses, 0,
                _due(now, minutes=LEARN_STEPS_MIN[nxt]),
            )
        return _result("review", 0, 1.0, ease, reps + 1, lapses, 0, _due(now, days=1))
    # easy：跳过剩下的学习步，直接毕业，起手 4 天
    return _result("review", 0, 4.0, ease, reps + 1, lapses, 0, _due(now, days=4))


def preview(row: Any, now: datetime | None = None) -> dict[str, str]:
    """四个档位各自会推到什么时候 —— 渲染在按钮上，点之前就能看见后果。"""
    now = now or _now()
    out: dict[str, str] = {}
    for g in GRADES:
        r = schedule(
            state=row["srs_state"], step=row["srs_step"], interval=row["srs_interval"],
            ease=row["srs_ease"], reps=row["srs_reps"], lapses=row["srs_lapses"],
            grade=g, now=now,
        )
        out[g] = human_interval(r["due"], now)
    return out


# ---------------------------------------------------------------- 视图组装

def _state_label(state: str) -> str:
    return {"new": "新词", "learning": "学习中", "review": "复习中"}.get(state, state)


def _days_label(days: float) -> str:
    """间隔天数的静态说法（不是「距今多久」，所以不走 human_interval）。"""
    if days < 1:
        return "1 天内"
    if days < 30:
        return f"{round(days)} 天"
    if days < 365:
        return f"{days / 30:.1f} 个月"
    return f"{days / 365:.1f} 年"


def _review_block(row: sqlite3.Row | dict) -> dict[str, Any]:
    d = dict(row)
    reps = int(d.get("srs_reps") or 0)
    lapses = int(d.get("srs_lapses") or 0)
    interval = float(d.get("srs_interval") or 0)
    state = d.get("srs_state") or "new"
    if state == "review" and interval:
        progress = f"第 {reps} 次复习 · 间隔 {_days_label(interval)}"
    elif state == "learning":
        # 学习阶段不说「已答对 N 次」：reps 只在毕业时 +1，刚答对第一遍它还是 0，
        # 写出来就是「已答对 0 次」，看着像记录丢了。说还剩几步更准。
        left = max(0, len(LEARN_STEPS_MIN) - int(d.get("srs_step") or 0))
        progress = f"学习中 · 再答对 {left} 次毕业"
    else:
        progress = "还没复习过"
    if lapses:
        progress += f" · 忘过 {lapses} 次"
    return {
        "state": state,
        "state_label": _state_label(state),
        "step": int(d.get("srs_step") or 0),
        "interval": interval,
        "ease": float(d.get("srs_ease") or EASE_START),
        "reps": reps,
        "lapses": lapses,
        "due": d.get("srs_due"),
        "due_label": human_interval(d.get("srs_due")),
        "last_at": d.get("srs_last_at"),
        "suspended": bool(d.get("srs_suspended")),
        "progress": progress,
    }


def card(vocab_id: int) -> dict | None:
    """一张复习卡：生词本条目 + 复习排期 + 四个按钮的间隔。"""
    row = vocab.get_vocab(vocab_id)
    if row is None:
        return None
    with read() as conn:
        raw = conn.execute("SELECT * FROM vocab WHERE id = ?", (vocab_id,)).fetchone()
    row["review"] = _review_block(raw)
    row["intervals"] = preview(raw)
    return row


def queue(
    *,
    limit: int = DEFAULT_LIMIT,
    new_limit: int = DEFAULT_NEW_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """今天该复习的卡。

    排序：到期越早越靠前（欠得最久的先还），同档里 worth 高的先来。
    新词单独限量并排在所有到期旧词之后 —— 旧词是先欠下的。
    """
    now = now or _now()
    stamp = _iso(now)
    sql_tail = "ORDER BY v.srs_due ASC, v.worth DESC, v.id ASC LIMIT ?"

    with read() as conn:
        due_total = conn.execute(
            "SELECT COUNT(*) c FROM vocab v WHERE v.srs_suspended = 0 AND srs_due <= ?",
            (stamp,),
        ).fetchone()["c"]
        new_total = conn.execute(
            "SELECT COUNT(*) c FROM vocab v"
            " WHERE v.srs_suspended = 0 AND v.srs_state = 'new'",
        ).fetchone()["c"]
        next_row = conn.execute(
            "SELECT MIN(srs_due) d FROM vocab v"
            " WHERE v.srs_suspended = 0 AND srs_due > ?",
            (stamp,),
        ).fetchone()
        # 暂停之外的词（含 new）一个不剩 —— 用于「今日完成」的文案
        rest_row = conn.execute(
            "SELECT MIN(srs_due) d FROM vocab v"
            " WHERE v.srs_suspended = 0 AND srs_state != 'new' AND srs_due > ?",
            (stamp,),
        ).fetchone()

        cols = f"{vocab.SELECT_COLS},{vocab._SOURCES_SUB}"
        # 先用掉额度的「老债」
        rows = conn.execute(
            f"SELECT {cols} FROM vocab v"
            " WHERE v.srs_suspended = 0 AND v.srs_due <= ? AND v.srs_state != 'new'"
            f" {sql_tail}",
            [None, stamp, limit],
        ).fetchall()
        items = [dict(r) for r in rows]

        remain = limit - len(items)
        if remain > 0:
            new_rows = conn.execute(
                f"SELECT {cols} FROM vocab v"
                " WHERE v.srs_suspended = 0 AND v.srs_due <= ? AND v.srs_state = 'new'"
                f" {sql_tail}",
                [None, stamp, min(remain, new_limit)],
            ).fetchall()
            items.extend(dict(r) for r in new_rows)

        # 卡片背面要写「这条例句出自哪篇」。出处用的是**卡片上那句**的来源，
        # 不是 vocab.article_id（那是首次入册那篇，两篇文章划同一个词时会分叉）。
        vocab._sentence_sources(conn, items, None)
        # 正面那两个喇叭（英美）一次装好 —— 否则 `_row()` 会逐张卡去查，
        # 一队 60 张就是 120 次短连接。
        vocab._attach_audios(conn, items)

    # 每张卡带上排期与四个按钮的间隔（与真正评分共用 schedule，不会对不上）
    out = []
    for it in items:
        base = vocab._row(it)
        base["review"] = _review_block(it)
        base["intervals"] = preview(it, now)
        out.append(base)

    return {
        "items": out,
        "due_total": int(due_total),
        "new_total": int(new_total),
        "limit": limit,
        "new_limit": new_limit,
        "next_due": next_row["d"] if next_row else None,
        "next_after_queue": rest_row["d"] if rest_row else None,
    }


def grade(
    vocab_id: int, grade_name: str, *, elapsed_ms: int | None = None,
    now: datetime | None = None,
) -> dict | None:
    """评一次分：更新排期 + 写流水。两步必须在同一个事务里。

    返回评分后的卡（含新的四档间隔）。卡不存在返回 None。
    """
    if grade_name not in GRADES:
        raise ValueError(f"未知的评分：{grade_name}（可选 {'/'.join(GRADES)}）")

    now = now or _now()
    with tx() as conn:
        row = conn.execute("SELECT * FROM vocab WHERE id = ?", (vocab_id,)).fetchone()
        if row is None:
            return None

        res = schedule(
            state=row["srs_state"], step=row["srs_step"], interval=row["srs_interval"],
            ease=row["srs_ease"], reps=row["srs_reps"], lapses=row["srs_lapses"],
            grade=grade_name, now=now,
        )
        conn.execute(
            "UPDATE vocab SET srs_state = ?, srs_step = ?, srs_interval = ?,"
            " srs_ease = ?, srs_reps = ?, srs_lapses = ?, srs_due = ?, srs_last_at = ?,"
            " updated_at = ? WHERE id = ?",
            (
                res["state"], res["step"], res["interval"], res["ease"], res["reps"],
                res["lapses"], res["due"], _iso(now), _iso(now), vocab_id,
            ),
        )
        conn.execute(
            "INSERT INTO review_logs (vocab_id, grade, prev_state, next_state,"
            " prev_interval, next_interval, ease_after, was_lapse, due_after,"
            " elapsed_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                vocab_id, grade_name, row["srs_state"] or "new", res["state"],
                float(row["srs_interval"] or 0), res["interval"], res["ease"],
                res["was_lapse"], res["due"],
                int(elapsed_ms) if elapsed_ms else None, _iso(now),
            ),
        )
    return card(vocab_id)


def set_suspended(vocab_id: int, suspended: bool) -> dict | None:
    """暂停 / 恢复。恢复时若从没排过期就补上「现在」。"""
    with tx() as conn:
        row = conn.execute(
            "SELECT id, srs_due FROM vocab WHERE id = ?", (vocab_id,)
        ).fetchone()
        if row is None:
            return None
        if suspended:
            conn.execute(
                "UPDATE vocab SET srs_suspended = 1, updated_at = ? WHERE id = ?",
                (now_iso(), vocab_id),
            )
        else:
            conn.execute(
                "UPDATE vocab SET srs_suspended = 0, srs_due = COALESCE(srs_due, ?),"
                " updated_at = ? WHERE id = ?",
                (now_iso(), now_iso(), vocab_id),
            )
    return card(vocab_id)


# ---------------------------------------------------------------- 统计

def stats(now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    stamp = _iso(now)
    today = now.date().isoformat()

    with read() as conn:
        rows = conn.execute(
            "SELECT srs_state, srs_due, srs_suspended FROM vocab"
        ).fetchall()
        by_state = {"new": 0, "learning": 0, "review": 0}
        due_now = new_available = suspended = 0
        buckets: dict[str, int] = {}
        for r in rows:
            if r["srs_suspended"]:
                suspended += 1
                continue
            st = r["srs_state"] or "new"
            by_state[st] = by_state.get(st, 0) + 1
            if st == "new":
                new_available += 1
            due = r["srs_due"]
            if due is None:
                continue
            if due <= stamp:
                due_now += 1
            if st != "new":
                # 「今天」这一格把逾期的一起并进来 —— 逾期就是今天该还的。
                # 只统计非 new：新词没到期这回事，它们进预测柱会把图撑变形。
                day = due[:10] if due > stamp else today
                buckets[day] = buckets.get(day, 0) + 1

        rev = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(grade = 'again'), 0) again,"
            " COALESCE(SUM(was_lapse), 0) lapses FROM review_logs"
            " WHERE substr(created_at, 1, 10) = ?",
            (today,),
        ).fetchone()
        week = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(grade = 'again'), 0) again"
            " FROM review_logs WHERE created_at >= ?",
            (_iso(now - timedelta(days=7)),),
        ).fetchone()

    forecast = []
    for i in range(7):
        day = (now.date() + timedelta(days=i)).isoformat()
        forecast.append({"date": day, "due": int(buckets.get(day, 0)) if i else int(due_now)})

    week_n = int(week["n"])
    retention = None
    if week_n:
        retention = round(1 - int(week["again"]) / week_n, 4)

    return {
        "total": len(rows),
        "due_now": int(due_now),
        "new_available": int(new_available),
        "suspended": int(suspended),
        "by_state": by_state,
        "today": {
            "reviews": int(rev["n"]),
            "again": int(rev["again"]),
            "lapses": int(rev["lapses"]),
        },
        "retention_7d": retention,
        "week_reviews": week_n,
        "forecast": forecast,
        "interval_cap_days": MAX_INTERVAL_DAYS,
        "learn_steps_min": list(LEARN_STEPS_MIN),
    }


def list_logs(
    vocab_id: int | None = None, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    where, params = [], []
    if vocab_id is not None:
        where.append("l.vocab_id = ?")
        params.append(vocab_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM review_logs l {clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT l.*, v.lemma FROM review_logs l"
            " LEFT JOIN vocab v ON v.id = l.vocab_id"
            f" {clause} ORDER BY l.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_NEW_LIMIT",
    "EASE_MAX",
    "EASE_MIN",
    "EASE_START",
    "GRADES",
    "GRADE_LABEL",
    "LEARN_STEPS_MIN",
    "MAX_INTERVAL_DAYS",
    "card",
    "grade",
    "human_interval",
    "list_logs",
    "preview",
    "queue",
    "schedule",
    "set_suspended",
    "stats",
]
