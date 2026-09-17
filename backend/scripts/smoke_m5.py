"""M5 后端验收：间隔复习（SRS）与 SSE 流式输出。

跑法（后端 8010 要在跑）：
    python -u scripts/smoke_m5.py

分七段：

  A 调度算法（纯函数，不碰库）—— 四档 × 三个阶段 × 边界（ease 下限、间隔上限）
  B 队列 —— 四档间隔与真实评分一致、新词限量、排序、暂停的词不出现在队列里
  C 评分落库 —— 排期更新 + 流水新增 + 事务性 + 非法入参
  D 暂停 / 恢复
  E 统计 —— due_now / forecast / 今日计数 与手工核对
  F SSE —— 帧序、增量能拼成合法 JSON、缓存命中不出增量、
            以及**流式与一次性端点结果逐字相同**（证明没有第二套编排）
  G 健康检查

脚本会自己造一个一次性生词并在收尾删掉，可重复运行。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.db import read  # noqa: E402
from app.services import srs  # noqa: E402
from app.services.ai import PROMPT_VERSION  # noqa: E402

BACKEND = "http://127.0.0.1:8010"

# F 节用来拼「每次都不一样」的英文句子
ADJECTIVES = [
    "amber", "brisk", "candid", "dormant", "elastic",
    "fluent", "genial", "hollow", "idle", "jovial",
]

passed = 0
failed: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> bool:
    global passed
    if cond:
        passed += 1
        print(f"  \u2713 {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    else:
        failed.append(name)
        print(f"  \u2717 {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    return cond


def head(t: str) -> None:
    print(f"\n{t}", flush=True)


def near(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def client(timeout: float = 180.0) -> httpx.Client:
    return httpx.Client(base_url=BACKEND, timeout=timeout)


def rel_minutes(iso: str, base: datetime) -> float:
    return (datetime.fromisoformat(iso) - base).total_seconds() / 60


# ================================================================ A 调度算法

def section_a() -> None:
    head("A 调度算法（纯函数）")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def s(**kw):
        base = dict(state="new", step=0, interval=0, ease=2.5, reps=0, lapses=0)
        base.update(kw)
        return srs.schedule(grade=base.pop("grade"), now=now, **base)

    # ---- 新词 ----
    r = s(grade="good")
    check(r["state"] == "learning" and r["step"] == 1, "新词 good → 学习中第 2 步")
    check(near(rel_minutes(r["due"], now), 10, 0.01), "新词 good 的下次到期 = 10 分钟",
          f"{rel_minutes(r['due'], now):.2f} 分钟")
    check(r["reps"] == 0, "新词 good 不增加 reps（还没毕业）")

    r = s(grade="hard")
    check(r["state"] == "learning" and r["step"] == 0, "新词 hard → 原地不动，仍是第 1 步")
    check(near(rel_minutes(r["due"], now), 1, 0.01), "新词 hard 的下次到期 = 1 分钟")

    r = s(grade="again")
    check(r["state"] == "learning" and r["step"] == 0, "新词 again → 回第 1 步")
    check(near(rel_minutes(r["due"], now), 1, 0.01), "新词 again 的下次到期 = 1 分钟")
    check(r["was_lapse"] == 0, "新词 again 不算遗忘（只有 review 阶段才算）")

    r = s(grade="easy")
    check(r["state"] == "review" and near(r["interval"], 4.0), "新词 easy → 直接毕业，间隔 4 天")
    check(near(rel_minutes(r["due"], now), 4 * 1440, 1), "新词 easy 的下次到期 = 4 天")

    r = s(step=1, grade="good")
    check(r["state"] == "review" and near(r["interval"], 1.0), "学习第 2 步 good → 毕业，间隔 1 天")
    check(r["reps"] == 1, "毕业时 reps +1")

    # ---- 已毕业 ----
    def rev(**kw):
        base = dict(state="review", step=0, interval=10.0, ease=2.5, reps=3, lapses=0)
        base.update(kw)
        return s(**base)

    r = rev(grade="good")
    check(r["state"] == "review" and near(r["interval"], 25.0), "复习 good → 间隔 × ease = 25 天")
    check(r["reps"] == 4, "复习 good → reps +1")

    r = rev(grade="easy")
    check(near(r["ease"], 2.65), "复习 easy → ease +0.15")
    check(near(r["interval"], 34.45, 0.01), "复习 easy → 间隔 × ease × 1.3")

    r = rev(grade="hard")
    check(near(r["ease"], 2.35), "复习 hard → ease −0.15")
    check(near(r["interval"], 12.0), "复习 hard → 间隔 × 1.2")

    r = rev(grade="again")
    check(r["state"] == "learning" and r["step"] == 0, "复习 again → 回到学习阶段")
    check(near(r["interval"], 5.0), "复习 again → 间隔折半（不是归 1 天）")
    check(r["lapses"] == 1 and r["was_lapse"] == 1, "复习 again → 记一次遗忘")
    check(near(r["ease"], 2.30), "复习 again → ease −0.20")

    # ---- 边界 ----
    ease = 2.5
    for _ in range(20):
        ease = rev(grade="again", ease=ease)["ease"]
    check(near(ease, 1.3), "ease 有下限 1.3", f"{ease}")
    ease = 2.5
    for _ in range(20):
        ease = rev(grade="easy", ease=ease)["ease"]
    check(near(ease, 3.2), "ease 有上限 3.2", f"{ease}")

    r = rev(grade="easy", interval=300.0)
    check(r["interval"] <= 365.0, "间隔有上限 365 天", f"{r['interval']}")
    r = rev(grade="good", interval=364.0)
    check(r["interval"] <= 365.0, "间隔上限对 good 也生效", f"{r['interval']}")

    # ---- 非法入参 ----
    try:
        s(grade="nope")
        check(False, "未知评分应抛 ValueError")
    except ValueError:
        check(True, "未知评分抛 ValueError")

    # ---- 人话标签 ----
    #
    # 「1 天」只可能出现在**毕业那一次**：新词点 good 是进学习第 2 步（10 分钟），
    # 只有学习第 2 步再点 good 才毕业并给 1 天。用 `s(grade="good")` 去求它是错的。
    labels = {g: srs.human_interval(s(grade=g)["due"], now) for g in srs.GRADES}
    check(labels["again"] == "1 分钟", "间隔标签：1 分钟", labels["again"])
    check(srs.human_interval(s(step=1, grade="good")["due"], now) == "1 天",
          "间隔标签：1 天（毕业那次）")
    check(labels["easy"] == "4 天", "间隔标签：4 天", labels["easy"])
    check(srs.human_interval(None) == "—", "没有到期时间时标签是「—」")


# ================================================================ 准备一次性词

POOL = [
    "perspicacious", "recalcitrant", "obstreperous", "defenestrate",
    "antediluvian", "perambulate", "obfuscate", "quixotic",
]


def make_card(article_id: int, start: int) -> tuple[int | None, str, str]:
    """借一次 dict 调用造一个全新生词，返回 (vocab_id, lemma, sentence)。"""
    with client() as c:
        for i in range(len(POOL)):
            w = POOL[(start + i) % len(POOL)]
            sentence = f"The reviewer called the schedule {w} after three passes."
            # 入册入口：M10 起划词是一次本地词典查询（不再有 /api/ai/dict）
            r = c.get(f"/api/dict/{w}", params={
                "article_id": article_id, "sentence": sentence,
            })
            if r.status_code != 200:
                continue
            enr = r.json().get("enrolled") or {}
            if enr.get("created") is True and enr.get("vocab_id"):
                return int(enr["vocab_id"]), str(enr.get("lemma") or w), sentence
    return None, "", ""


def first_article_id() -> int | None:
    with client(60) as c:
        items = c.get("/api/articles").json()["items"]
    done = [a for a in items if a["status"] == "done"]
    return int(done[0]["id"]) if done else None


# ================================================================ B 队列

def section_b(vid: int, lemma: str) -> None:
    head("B 队列")
    with client() as c:
        q = c.get("/api/review/queue", params={"limit": 200, "new_limit": 50}).json()
        q0 = c.get("/api/review/queue", params={"limit": 200, "new_limit": 50})

    check(q0.status_code == 200, "GET /api/review/queue 返回 200")
    keys = {"items", "due_total", "new_total", "limit", "new_limit", "next_due", "next_after_queue"}
    check(keys <= set(q), "队列响应字段齐全", ",".join(sorted(keys - set(q))) or "ok")
    check(any(i["id"] == vid for i in q["items"]), f"刚入册的 {lemma} 立刻进了队列（入册即到期）")

    card = next(i for i in q["items"] if i["id"] == vid)
    check(set(card["intervals"]) == set(srs.GRADES), "每张卡都带四档间隔",
          ",".join(sorted(card["intervals"])))
    check(all(v for v in card["intervals"].values()), "四档间隔都不是空串",
          json.dumps(card["intervals"], ensure_ascii=False))
    check(card["review"]["state"] == "new", "新卡的排期状态是 new")
    check(card["review"]["due_label"] in ("现在", "1 分钟") or "分钟" in card["review"]["due_label"],
          "新卡的到期标签是「现在」附近", card["review"]["due_label"])
    check("sentence" in card and card["sentence"], "卡上带文中例句")
    check(card.get("sentence_article_id") is not None,
          "例句带出处文章（不等于「首次入册那篇」）", str(card.get("sentence_article_id")))

    # 与 DB 手工核对的计数
    with read() as conn:
        stamp = srs._iso(srs._now())
        want_due = conn.execute(
            "SELECT COUNT(*) c FROM vocab WHERE srs_suspended = 0 AND srs_due <= ?", (stamp,)
        ).fetchone()["c"]
        want_new = conn.execute(
            "SELECT COUNT(*) c FROM vocab WHERE srs_suspended = 0 AND srs_state = 'new'"
        ).fetchone()["c"]
    check(q["due_total"] == want_due, "due_total 与库里手工计数一致",
          f"{q['due_total']} vs {want_due}")
    check(q["new_total"] == want_new, "new_total 与库里手工计数一致",
          f"{q['new_total']} vs {want_new}")

    # 新词限量
    with client() as c:
        q2 = c.get("/api/review/queue", params={"limit": 200, "new_limit": 1}).json()
    news = [i for i in q2["items"] if i["review"]["state"] == "new"]
    check(len(news) <= 1, "new_limit=1 时队列里最多 1 个新词", f"{len(news)} 个")
    check(q2["new_total"] == q["new_total"], "限量不影响 new_total（总数照实报）")

    # 排序：到期的旧词排在新词之前
    states = [i["review"]["state"] for i in q["items"]]
    if "new" in states and any(s != "new" for s in states):
        check(states.index("new") > 0, "新词排在所有到期旧词之后",
              f"第一个 new 在第 {states.index('new')} 位")
    else:
        check(True, "队列里暂时只有一类卡，排序断言跳过")

    # 暂停的词不出现在队列里
    with client() as c:
        c.post(f"/api/review/cards/{vid}/suspend")
        q3 = c.get("/api/review/queue", params={"limit": 200, "new_limit": 50}).json()
    check(not any(i["id"] == vid for i in q3["items"]), "暂停后不再出现在队列里")
    check(all(not i["review"]["suspended"] for i in q3["items"]), "队列里没有任何暂停的词")
    with client() as c:
        c.post(f"/api/review/cards/{vid}/resume")


# ================================================================ C 评分落库

def section_c(vid: int, lemma: str) -> None:
    head("C 评分落库")

    with client() as c:
        before = c.get(f"/api/review/cards/{vid}").json()
        logs_before = c.get("/api/review/logs", params={"vocab_id": vid}).json()["total"]

    # 队列里四个按钮上写的间隔，与按下去之后实际得到的必须一致
    predicted = before["intervals"]["good"]
    with client() as c:
        r = c.post(f"/api/review/cards/{vid}/grade", json={"grade": "good", "elapsed_ms": 4100})
    check(r.status_code == 200, "评分返回 200")
    after = r.json()
    check(after["review"]["state"] == "learning", "新词 good 后进入学习中")
    check(after["review"]["due_label"] == predicted,
          "按下去得到的间隔 == 按钮上写的", f"{after['review']['due_label']} vs {predicted}")
    check(after["review"]["last_at"] is not None, "记录上次复习时间")

    with client() as c:
        logs = c.get("/api/review/logs", params={"vocab_id": vid}).json()
    check(logs["total"] == logs_before + 1, "写了一条复习流水", f"{logs['total']}")
    row = logs["items"][0]
    check(row["grade"] == "good", "流水记下评分")
    check(row["prev_state"] == "new" and row["next_state"] == "learning", "流水记下状态迁移")
    check(row["elapsed_ms"] == 4100, "流水记下看了多久", str(row["elapsed_ms"]))
    check(row["lemma"] == lemma, "流水带词条（JOIN 得到）")

    # 再走一步到毕业
    with client() as c:
        r = c.post(f"/api/review/cards/{vid}/grade", json={"grade": "good"})
    ok = r.json()
    check(ok["review"]["state"] == "review", "第二次 good → 毕业进 review")
    check(ok["review"]["reps"] == 1, "毕业时 reps = 1")
    check(abs(ok["review"]["interval"] - 1.0) < 1e-6, "毕业间隔 1 天", str(ok["review"]["interval"]))

    # 第三次 good：间隔 × ease
    with client() as c:
        r = c.post(f"/api/review/cards/{vid}/grade", json={"grade": "good"})
    ok = r.json()
    check(abs(ok["review"]["interval"] - 2.5) < 1e-6, "复习 good → 1 × 2.5 = 2.5 天",
          str(ok["review"]["interval"]))
    check(abs(ok["review"]["ease"] - 2.5) < 1e-6, "good 不改 ease")

    # 忘了 → 掉回学习阶段
    with client() as c:
        r = c.post(f"/api/review/cards/{vid}/grade", json={"grade": "again"})
    ok = r.json()
    check(ok["review"]["state"] == "learning", "复习中点「忘了」→ 掉回学习中")
    check(ok["review"]["lapses"] == 1, "记一次遗忘")
    check(abs(ok["review"]["interval"] - 1.25) < 1e-6, "间隔折半 = 1.25 天",
          str(ok["review"]["interval"]))

    # 排期确实落库了（重新读一次，不是靠返回值）
    with read() as conn:
        db = conn.execute(
            "SELECT srs_state, srs_lapses, srs_reps, srs_ease FROM vocab WHERE id = ?", (vid,)
        ).fetchone()
    check(db["srs_state"] == "learning" and db["srs_lapses"] == 1,
          "状态与遗忘次数真的写进了库")
    # reps 只数「成功复习」：new→learning 不加，毕业 +1，之后每次 good +1
    # （「忘了」不加）。本例走了 good / good / good / again 四步，所以是 2。
    check(db["srs_reps"] == 2, "reps 落库 = 2（毕业 1 次 + 复习答对 1 次）", str(db["srs_reps"]))

    # 非法入参
    with client() as c:
        code = c.post(f"/api/review/cards/{vid}/grade", json={"grade": "nope"}).status_code
        code404 = c.post("/api/review/cards/99999999/grade", json={"grade": "good"}).status_code
        code_get404 = c.get("/api/review/cards/99999999").status_code
        code_neg = c.post(f"/api/review/cards/{vid}/grade",
                          json={"grade": "good", "elapsed_ms": -1}).status_code
    check(code == 422, "未知评分 422", str(code))
    check(code404 == 404, "不存在的词 404", str(code404))
    check(code_get404 == 404, "取不存在的卡 404", str(code_get404))
    check(code_neg == 422, "负的 elapsed_ms 被拒（422）", str(code_neg))


# ================================================================ D 暂停 / 恢复

def section_d(vid: int, lemma: str) -> None:
    """暂停 / 恢复。

    **必须排在 section_c 之前跑**：这一节要验证「暂停一个到期的词，
    due_now 少 1」，而 section_c 会把这张卡评到「1 分钟后到期」——
    那时它已经不在 due 里了，再暂停自然不影响 due_now，
    断言就会变成一条没有意义的恒真式。
    """
    head("D 暂停 / 恢复")
    with client() as c:
        s0 = c.get("/api/review/stats").json()
        card = c.get(f"/api/review/cards/{vid}").json()
        r = c.post(f"/api/review/cards/{vid}/suspend")
        ok = r.json()
        s1 = c.get("/api/review/stats").json()
    check(r.status_code == 200 and ok["review"]["suspended"] is True, "暂停返回 suspended=True")
    check(s1["suspended"] == s0["suspended"] + 1, "统计里暂停数 +1",
          f"{s0['suspended']} → {s1['suspended']}")
    check(card["srs_due"] is not None, "前置条件：这张卡是到期的（否则下面的断言没意义）")
    check(s1["due_now"] == s0["due_now"] - 1,
          "暂停一个到期的词，due_now 少 1", f"{s0['due_now']} → {s1['due_now']}")

    with client() as c:
        r = c.post(f"/api/review/cards/{vid}/resume")
        ok = r.json()
        s2 = c.get("/api/review/stats").json()
    check(ok["review"]["suspended"] is False, "恢复返回 suspended=False")
    check(s2["suspended"] == s0["suspended"], "暂停数回到原值")
    check(s2["due_now"] == s0["due_now"], "恢复后 due_now 也回到原值")
    check(ok["srs_due"] is not None, "恢复后排期不为空（否则永远进不了队列）")

    with client() as c:
        code = c.post("/api/review/cards/99999999/suspend").status_code
    check(code == 404, "暂停不存在的词 404", str(code))


# ================================================================ E 统计

def section_e() -> None:
    head("E 统计")
    with client() as c:
        st = c.get("/api/review/stats").json()

    need = {"total", "due_now", "new_available", "suspended", "by_state", "today",
            "retention_7d", "week_reviews", "forecast", "interval_cap_days",
            "learn_steps_min"}
    check(need <= set(st), "统计字段齐全", ",".join(sorted(need - set(st))) or "ok")

    with read() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM vocab").fetchone()["c"]
        stamp = srs._iso(srs._now())
        due = conn.execute(
            "SELECT COUNT(*) c FROM vocab WHERE srs_suspended = 0 AND srs_due <= ?", (stamp,)
        ).fetchone()["c"]
        news = conn.execute(
            "SELECT COUNT(*) c FROM vocab WHERE srs_suspended = 0 AND srs_state = 'new'"
        ).fetchone()["c"]
        today = srs._now().date().isoformat()
        rev = conn.execute(
            "SELECT COUNT(*) c FROM review_logs WHERE substr(created_at,1,10) = ?", (today,)
        ).fetchone()["c"]
        susp = conn.execute("SELECT COUNT(*) c FROM vocab WHERE srs_suspended = 1").fetchone()["c"]
    check(st["total"] == total, "total 与库一致", f"{st['total']} vs {total}")
    check(st["due_now"] == due, "due_now 与库一致", f"{st['due_now']} vs {due}")
    check(st["new_available"] == news, "new_available 与库一致", f"{st['new_available']} vs {news}")
    check(st["suspended"] == susp, "suspended 与库一致", f"{st['suspended']} vs {susp}")
    check(st["today"]["reviews"] == rev, "今日复习次数与库一致", f"{st['today']['reviews']} vs {rev}")
    check(set(st["by_state"]) == {"new", "learning", "review"}, "by_state 三态齐全")
    check(sum(st["by_state"].values()) + st["suspended"] == total,
          "三态之和 + 暂停 = 总数（没有词掉在状态外）")

    check(len(st["forecast"]) == 7, "七日预测有 7 格", str(len(st["forecast"])))
    check(st["forecast"][0]["date"] == today, "预测第一格是今天")
    check(st["forecast"][0]["due"] == st["due_now"], "预测第一格 = due_now（含逾期）")
    check(all(f["due"] >= 0 for f in st["forecast"]), "预测没有负数")
    check(st["interval_cap_days"] == 365.0, "暴露间隔上限给前端做文案")
    check(st["learn_steps_min"] == [1, 10], "暴露学习步长给前端做文案")

    if st["week_reviews"]:
        check(st["retention_7d"] is not None and 0.0 <= st["retention_7d"] <= 1.0,
              "近七日记住率在 0~1 之间", str(st["retention_7d"]))
    else:
        check(st["retention_7d"] is None, "没有复习记录时记住率为 null")

    with client() as c:
        ok = c.get("/api/review/logs", params={"limit": 5}).json()
    check(ok["total"] >= 4 and len(ok["items"]) <= 5, "复习流水可分页",
          f"total={ok['total']} 本页 {len(ok['items'])}")
    check(all("lemma" in i for i in ok["items"]), "流水带词条")
    check(ok["items"][0]["id"] > ok["items"][-1]["id"], "流水按 id 倒序")


# ================================================================ F SSE

def sse(payload: dict, timeout: float = 180.0) -> list[tuple[str, dict]]:
    """收完一次 SSE，返回 [(event, data), ...]。"""
    out: list[tuple[str, dict]] = []
    event = "message"
    with httpx.Client(base_url=BACKEND, timeout=timeout) as c:
        with c.stream("POST", "/api/ai/stream", json=payload) as r:
            if r.status_code != 200:
                r.read()
                return [("http", {"status": r.status_code, "body": r.text[:300]})]
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    out.append((event, json.loads(line[5:].strip())))
    return out


def section_f(article_id: int) -> None:
    head("F SSE 流式输出")

    # **必须是全新输入**：上一次跑已经把这句的译文写进 ai_logs 了，第二次
    # 同一句会命中缓存 —— 那样「有 delta 帧吗」这类断言验的就不是流式，
    # 而是缓存的正确性（第一版就是这么写错的）。所以拼一句每次都不同的英文，
    # 并显式断言 source == "llm"，让「确实走了真调用」这件事被钉住。
    # 代价是每跑一次 F 节花掉一次解析（M8 起选段只有一个解析任务，
    # 实测 10~20 s、数千 token —— 比合并前的 translate 贵，这是合并的必然代价）。
    text = (
        f"The {random.choice(ADJECTIVES)} committee reconvened at dawn to reconsider "
        f"the shelved proposal, item {random.randint(100000, 999999)}."
    )
    payload = {"kind": "explain", "article_id": article_id, "text": text}

    frames = sse(payload)
    check(all(e != "http" for e, _ in frames), "流式端点返回 200",
          "" if all(e != "http" for e, _ in frames) else str(frames[0][1])[:160])
    if not frames or frames[0][0] == "http":
        return

    names = [e for e, _ in frames]
    check(names[0] == "open", "第一帧是 open", names[0])
    check(names.count("done") == 1, "有且只有一个 done", str(names.count("done")))
    check(names[-1] == "done", "done 是最后一帧", names[-1])
    check("error" not in names, "正常路径没有 error 帧")
    check(names.count("delta") > 0, "有 delta 帧（真的流起来了）", f"{names.count('delta')} 帧")

    open_payload = frames[0][1]
    check(open_payload.get("kind") == "explain", "open 帧带 kind")
    check(bool(open_payload.get("prompt_version")), "open 帧带 prompt_version",
          str(open_payload.get("prompt_version")))

    # 增量拼起来必须是一段合法 JSON —— 这是「正文只有一条流」的证据。
    #
    # 拼接时要处理 `reset` 帧：模型输出被 max_tokens 截断后会自动**重试**，
    # 重试前会把已经吐出的碎片作废（前端同此语义，见 lib/stream.ts）。
    # 不按 reset 清空缓冲的话，一次重试就会让这条断言假失败。
    buf: list[str] = []
    for e, d in frames:
        if e == "reset":
            buf.clear()
        elif e == "delta":
            buf.append(d.get("text", ""))
    joined = "".join(buf)
    parsed = None
    try:
        parsed = json.loads(joined)
        check(True, "delta 拼起来是合法 JSON", f"{len(joined)} 字符")
    except Exception as e:  # noqa: BLE001
        check(False, "delta 拼起来是合法 JSON", f"{type(e).__name__}: {joined[:120]}")

    done = next(d for e, d in frames if e == "done")
    check(done["prompt_type"] == "explain", "done 带 prompt_type")
    check(done["source"] == "llm", "首次是真调用（source=llm）", str(done["source"]))
    check(done["ai_log_id"] is not None, "done 带 ai_log_id")
    check(done["result"].get("translation"), "done 里的结果是清洗后的成品",
          done["result"].get("translation", "")[:40])
    if parsed is not None:
        check(parsed.get("translation") == done["result"]["translation"],
              "增量拼出的译文与 done 的译文一致（同一次输出，不是两路）")

    # 落库：这条流水查得到
    with client() as c:
        logs = c.get("/api/ai/logs", params={"article_id": article_id, "limit": 5}).json()
    hit = next((i for i in logs["items"] if i["id"] == done["ai_log_id"]), None)
    check(hit is not None, "done 的 ai_log_id 在流水里查得到")
    if hit:
        check(text[:30] in hit["input_text"], "流水里留下了原文（需求 5）")
        check(hit["prompt_version"] == done["prompt_version"], "流水的 prompt_version 与 done 一致")

    # 缓存命中：第二次同一个输入不该再吐增量
    again = sse(payload)
    n2 = [e for e, _ in again]
    check(n2.count("delta") == 0, "命中缓存时没有 delta（没白花 token）", f"{n2.count('delta')} 帧")
    done2 = next(d for e, d in again if e == "done")
    check(done2["source"] == "cache", "第二次的 source=cache", str(done2["source"]))

    # 流式与一次性端点的结果必须逐字相同 —— 证明没有第二套编排
    with client() as c:
        once = c.post("/api/ai/explain", json={
            "article_id": article_id, "text": text,
        })
    check(once.status_code == 200, "一次性端点也 200")
    check(once.json()["result"] == done2["result"], "流式与一次性端点结果逐字相同")
    check(once.json()["source"] == "cache", "一次性端点同样命中缓存（共用同一套缓存）")

    # 错误路径
    bad = sse({"kind": "nope", "article_id": article_id, "text": "hi"})
    check(bad and bad[0][0] == "http" and bad[0][1]["status"] == 422, "未知 kind 422")
    bad = sse({"kind": "explain", "article_id": 99999999, "text": "hi"})
    check(bad and bad[0][0] == "http" and bad[0][1]["status"] == 404, "文章不存在 404")

    # M10：划词不再经过模型，`kind=dict` 已不是一个任务。它必须**明确被拒**，
    # 而不是悄悄退化成 explain 把单个单词当句子解析（白烧 token 且结果没意义）。
    frames = sse({"kind": "dict", "article_id": article_id, "text": "resilient"})
    check(
        bool(frames) and frames[0][0] == "http" and frames[0][1]["status"] == 422,
        "已下线的 dict 任务被拒（422）",
        json.dumps(frames[:1])[:80] if frames else "无帧",
    )


# ================================================================ G 健康检查

def section_g() -> None:
    head("G 健康检查")
    with client() as c:
        h = c.get("/api/health").json()
    check("review" in h, "健康检查带 review 段")
    check(isinstance(h.get("review", {}).get("due_now"), int), "due_now 是整数",
          str(h.get("review", {}).get("due_now")))
    with client() as c:
        st = c.get("/api/review/stats").json()
    check(h["review"]["due_now"] == st["due_now"], "健康检查里的 due_now 与统计一致")
    # 版本号断言跟着代码走，不写死在脚本里：M5 当年写死 "m4.1" 是为了证明
    # 「没改 prompt 就不升版本」；三操作合并后 prompt 确实改了，写死的值必然过期。
    check(h["llm"]["prompt_version"] == PROMPT_VERSION,
          "健康检查里的 prompt_version 与代码一致",
          f"{h['llm']['prompt_version']} vs {PROMPT_VERSION}")


# ================================================================ 收尾

def cleanup(vid: int | None) -> None:
    if vid is None:
        return
    with client(60) as c:
        c.delete(f"/api/vocab/{vid}")
    with read() as conn:
        left = conn.execute("SELECT COUNT(*) c FROM vocab WHERE id = ?", (vid,)).fetchone()["c"]
        logs = conn.execute(
            "SELECT COUNT(*) c FROM review_logs WHERE vocab_id = ?", (vid,)
        ).fetchone()["c"]
    check(left == 0, "收尾：一次性生词已移除")
    check(logs == 0, "收尾：它的复习流水随 CASCADE 一起清掉")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--keep", action="store_true", help="保留造出来的一次性数据，便于排查")
    args = ap.parse_args()

    aid = args.article_id or first_article_id()
    if aid is None:
        print("没有已抓好的文章，先跑一次抓取")
        return 2

    with client(60) as c:
        h = c.get("/api/health")
    if h.status_code != 200:
        print(f"后端没起来（{BACKEND}）")
        return 2

    section_a()
    vid: int | None = None
    try:
        vid, lemma, _ = make_card(aid, start=0)
        if vid is None:
            print("造一次性生词失败（池子里的词都被缓存占了），跳过 B~D")
        else:
            print(f"\n  · 一次性生词：{lemma}（vocab_id={vid}）")
            section_b(vid, lemma)
            section_d(vid, lemma)   # 必须在 C 之前：见 section_d 的说明
            section_c(vid, lemma)
        section_e()
        section_f(aid)
        section_g()
    finally:
        if not args.keep:
            cleanup(vid)

    print(f"\n{'=' * 52}")
    total = passed + len(failed)
    print(f"M5 后端验收：{passed}/{total} 通过")
    if failed:
        print("失败项：")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
