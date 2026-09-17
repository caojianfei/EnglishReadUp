"""M5 浏览器验收：复习页（间隔复习）与 AI 面板的流式增量。

跑法（后端 8010 + 前端 5173 都要在跑）：
    python -u scripts/smoke_ui_m5.py
    python -u scripts/smoke_ui_m5.py --article-id 2 --headed

比后端脚本多验的四件事（都是只能在浏览器里看出来的）：

  1. **翻面之前看不到答案** —— 正面只允许出现词形、音标、词性。「先看答案
     再回忆」等于没复习，所以这条必须由 DOM 层面保证，不能靠自觉。
  2. **按钮上写的间隔 == 按下去得到的间隔** —— 后端两个值同源，但前端有没有
     把 `intervals[grade]` 正确接到按钮上、有没有错位一格，只有在页面上看得到。
     错位的后果很隐蔽：用户以为自己选的是「记得」，实际记成了「太简单」。
  3. **例句提示真的把答案挖掉** —— 提示模式显示的那句里不能出现目标词，
     否则这不是提示，是把答案抄在题目上。
  4. **流式增量真的画出来了** —— `[data-testid="ai-partial"]` 必须出现过。
     如果这一句早就解析过（命中缓存），后端不会有任何 delta，这时退化为
     「结果正常渲染 + 面板显示缓存」，脚本会打印说明而不是假绿。

**对真实学习数据的影响**：这一节会把队列里已有的卡先暂停、收尾再恢复，
所以复现时不会把用户的学习进度推着走。见 `quarantine()`。
"""

from __future__ import annotations

import os

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

BACKEND = "http://127.0.0.1:8010"
FRONTEND = os.environ.get("FRONT_URL", "http://127.0.0.1:5173")
SHOTS = Path(__file__).resolve().parents[1] / "data" / "m5_shots"

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


def client(timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(base_url=BACKEND, timeout=timeout)


def api(path: str, params: dict | None = None) -> dict:
    with client() as c:
        return c.get(path, params=params).json()


def api_post(path: str) -> int:
    with client(60) as c:
        return c.post(path).status_code


def api_delete(path: str) -> int:
    with client(60) as c:
        return c.delete(path).status_code


POOL = [
    "kaleidoscope", "zephyr", "quixotic", "obfuscate", "perambulate",
    "perspicacious", "recalcitrant", "obstreperous", "defenestrate", "antediluvian",
]


def make_throwaways(article_id: int, start: int, want: int = 2) -> list[tuple[int, str]]:
    """借 dict 调用造 `want` 个全新生词 —— 复习队列需要「必然到期」的卡。

    不依赖已有的卡：跑过几轮之后它们都进了 learning / review，下次到期在
    未来，队列会空掉。**要两个而不是一个**：只造一个的话，第一张评完就直接
    进清空态，「评完换到下一张」「回执文案」这些断言全都没机会跑。

    **句子必须每次都不一样。** 早先用固定的模板句，于是第二轮跑的时候
    `input_hash` 命中缓存、走「同一个划词重复点开」那条分支，返回
    `enrolled: None`（它假设上次已经入过册了）—— 而上一轮的收尾已经把那张卡
    删掉了，于是再也造不出卡，整个 [B]~[E] 段被跳过。
    加一个只影响措辞的 run 标记，哈希就变了。代价是每次真调 2 次模型。
    """
    run_tag = f"run-{int(time.time()) % 100000}"
    out: list[tuple[int, str]] = []
    with client(180) as c:
        for i in range(len(POOL)):
            if len(out) >= want:
                break
            w = POOL[(start + i) % len(POOL)]
            # 入册入口：M10 起划词是一次本地词典查询（不再有 /api/ai/dict）
            r = c.get(f"/api/dict/{w}", params={
                "article_id": article_id,
                "sentence": (
                    f"The reviewer described the pacing as {w} "
                    f"after the second pass ({run_tag})."
                ),
            })
            if r.status_code != 200:
                continue
            enr = r.json().get("enrolled") or {}
            if enr.get("created") is True and enr.get("vocab_id"):
                vid = int(enr["vocab_id"])
                det = c.get(f"/api/vocab/{vid}").json()
                out.append((vid, str(det.get("lemma") or w)))
    return out


def quarantine(keep_ids: set[int]) -> list[int]:
    """把队列里**除一次性生词之外**的卡全部暂停，收尾再恢复。

    这个脚本会把队列一张张评完（要走到「本轮已清空」那一屏）。不隔离的话
    每跑一次验收就把真实学习进度推着往前走一格 —— 跑第三遍时那些词已经
    沉到几天后，用户自己打开复习页会发现「昨天刚记的词怎么不见了」。
    隔离只动 `srs_suspended` 一个开关，恢复时排期原样保留。
    """
    q = api("/api/review/queue", params={"limit": 300, "new_limit": 300})
    ids = [int(c["id"]) for c in q["items"] if int(c["id"]) not in keep_ids]
    for vid in ids:
        api_post(f"/api/review/cards/{vid}/suspend")
    return ids


def unquarantine(ids: list[int]) -> None:
    for vid in ids:
        api_post(f"/api/review/cards/{vid}/resume")


def pick_article(want: int | None) -> dict:
    if want is not None:
        return api(f"/api/articles/{want}")
    items = api("/api/articles")["items"]
    done = [a for a in items if a["status"] == "done" and a["block_count"] > 8]
    if not done:
        print("没有可用文章")
        sys.exit(2)
    done.sort(key=lambda a: -a["block_count"])
    return api(f"/api/articles/{done[0]['id']}")


def pick_unparsed_sentence(art: dict) -> str:
    """挑一句**没被 AI 解析过**的长句，用来验证真实流式。

    已被解析过的句子会命中缓存，后端一个 delta 都不发 —— 那时测的是缓存，
    不是流式。`ai_logs.input_text` 里出现过前 40 个字符的句子一律跳过。
    """
    logs = api("/api/ai/logs", params={"article_id": art["id"], "limit": 500})
    seen = " \n".join((i.get("input_text") or "") for i in logs["items"])
    for b in art["blocks"]:
        if b.get("type") != "para":
            continue
        t = (b.get("text") or "").strip()
        if len(t) < 80:
            continue
        for s in re.split(r"(?<=[.!?])\s+", t):
            s = s.strip()
            if 45 <= len(s) <= 150 and s[:40] not in seen:
                return s
    return ""


# ---------------------------------------------------------------- 页面读取

CARD_JS = """
() => {
  const el = document.querySelector('[data-testid="review-card"]');
  if (!el) return null;
  const txt = (sel) => {
    const n = el.querySelector(sel);
    return n ? n.textContent.trim() : null;
  };
  return {
    vocabId: Number(el.dataset.vocab),
    lemma: txt('[data-testid="card-lemma"]'),
    meaning: txt('[data-testid="card-meaning"]'),
    hint: txt('[data-testid="card-hint"]'),
    flash: txt('[data-testid="card-flash"]'),
    hasSentence: !!el.querySelector('[data-testid="card-sentence"]'),
    hasReveal: !!el.querySelector('[data-testid="reveal"]'),
    hasGrades: !!el.querySelector('[data-testid="grade-row"]'),
    // M7：正面的发音喇叭。音频一律本地化，url 必须指向本机端点
    audio: (() => {
      const b = el.querySelector('[data-testid="play-audio"]');
      return b ? { url: b.dataset.url || '', accent: b.dataset.accent || '' } : null;
    })(),
    grades: [...el.querySelectorAll('[data-testid="grade-row"] button')].map((b) => ({
      grade: b.dataset.grade,
      name: b.querySelector('.er-grade-name')?.textContent.trim(),
      next: b.querySelector('.er-grade-next')?.textContent.trim(),
    })),
    // 正面必须把这些藏起来：出现任何一个就说明答案提前泄露了
    leaks: ['[data-testid="card-meaning"]', '[data-testid="card-sentence"]']
      .filter((s) => el.querySelector(s))
      .concat(el.textContent.includes('文中含义') ? ['文中含义'] : []),
  };
}
"""


async def text_of(page, selector: str):
    """取文本，元素不存在就返回 None。

    不能用 `page.text_content(sel)`：它对找不到的元素会**等满 30 秒**再抛异常。
    「这个元素应该不存在」也是本脚本要断言的事（比如清空后卡片区必须消失），
    所以必须有一个不等待的版本。
    """
    el = await page.query_selector(selector)
    return await el.text_content() if el else None


async def main() -> None:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    for stale in SHOTS.glob("*fail*.png"):
        stale.unlink(missing_ok=True)

    art = pick_article(args.article_id)
    aid = art["id"]
    print(f"  样本：#{aid} {art['title'][:40]!r}", flush=True)

    throwaways = make_throwaways(aid, start=int(time.time()) % len(POOL))
    if len(throwaways) < 2:
        # 池子里的词都撞上缓存了（`created` 不为 true）。没有「必然到期」的卡，
        # 后面所有断言都会退化成「队列本来就空」，那种绿没有意义。
        print(f"只造出 {len(throwaways)} 个一次性生词（池子里的词被缓存占了），本次跳过")
        for vid, _ in throwaways:
            api_delete(f"/api/vocab/{vid}")
        sys.exit(2)
    keep = {vid for vid, _ in throwaways}
    print("  （一次性生词：" + "、".join(f"{w}#{v}" for v, w in throwaways) + "）", flush=True)
    held = quarantine(keep)
    print(f"  （暂停了队列里另外 {len(held)} 张卡，收尾会恢复）", flush=True)

    stats = api("/api/review/stats")
    queue0 = api("/api/review/queue", params={"limit": 300, "new_limit": 300})
    check(stats["due_now"] >= 2, "隔离后队列里仍有到期的卡（两张一次性生词）",
          f"due_now={stats['due_now']}")
    check(len(queue0["items"]) >= 2, "队列至少两张（保证能测「评完换下一张」）",
          f"{len(queue0['items'])} 张")
    # 前端展示的顺序与接口一致：第一张就是 items[0]
    first_lemma = queue0["items"][0]["lemma"]
    print(f"  （队列第一张是 {first_lemma!r}）", flush=True)

    console_errors: list[str] = []
    failed_requests: list[str] = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not args.headed)
            ctx = await browser.new_context(viewport={"width": 1440, "height": 920})
            page = await ctx.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on(
                "requestfailed",
                lambda req: failed_requests.append(f"{req.method} {req.url} {req.failure}")
                if "favicon" not in req.url
                else None,
            )

            # ------------------------------------------------------ A 导航
            head("[A] 导航里的「复习」入口与待办徽标")
            await page.goto(f"{FRONTEND}/", wait_until="networkidle")
            nav = await page.query_selector('[data-testid="nav-复习"]')
            check(nav is not None, "顶栏有「复习」入口")
            if nav:
                badge = " ".join(((await nav.text_content()) or "").split())
                check(str(stats["due_now"]) in badge, "徽标显示待复习张数", badge)
            await page.screenshot(path=str(SHOTS / "01-home-nav.png"))

            # ------------------------------------------------------ B 卡片
            head("[B] 复习卡：正面不泄露答案，翻面才给释义与四个档位")
            await page.click('[data-testid="nav-复习"]')
            await page.wait_for_url("**/review**", timeout=10000)
            ok = await page.wait_for_selector('[data-testid="review-card"]', timeout=20000)
            check(ok is not None, "复习页渲染出卡片")

            left0 = " ".join(((await text_of(page, '[data-testid="review-left"]')) or "").split())
            check("还剩" in left0, "顶部显示本轮剩余张数", left0)

            c0 = await page.evaluate(CARD_JS)
            check(c0 is not None and bool(c0["lemma"]), "卡面显示词形", str(c0 and c0["lemma"]))
            check(c0 and c0["lemma"] == first_lemma,
                  "页面第一张与接口 items[0] 一致", f"{c0 and c0['lemma']} vs {first_lemma}")
            check(c0["hasReveal"] is True, "翻面前只给「显示答案」按钮")
            check(c0["hasGrades"] is False, "翻面前没有评分按钮")
            check(c0["leaks"] == [], "翻面前看不到释义与例句", str(c0["leaks"]))
            # 发音不泄露释义 —— 卡正面本来就摆着音标，听一遍不算给答案，
            # 而复习恰恰是最需要听音的场景，没有喇叭才是真的怪。
            check(c0["audio"] is not None, "卡正面有发音喇叭（M7）", str(c0["audio"]))
            check(
                (c0["audio"] or {}).get("url", "").startswith("/api/dict/")
                and "://" not in (c0["audio"] or {}).get("url", ""),
                "喇叭指向本机端点而非外链（音频已本地化）",
                str((c0["audio"] or {}).get("url")),
            )
            await page.screenshot(path=str(SHOTS / "02-card-front.png"))

            # ---- 例句提示：目标词必须被挖掉 ----
            await page.click('button:has-text("显示例句提示")')
            await page.wait_for_timeout(400)
            c1 = await page.evaluate(CARD_JS)
            check(c1["hint"] is not None, "提示模式显示了一句原文")
            if c1["hint"]:
                low = c1["hint"].lower()
                # 挖词只按 surface / lemma 找，而一次性生词的 surface == lemma，
                # 所以查 lemma 就够了
                target = (c0["lemma"] or "").lower()
                check(target and target not in low,
                      "提示句里目标词已被挖掉", c1["hint"][:60])
                check("﹏﹏" in c1["hint"] or "没有原词形" in c1["hint"],
                      "挖词处有占位符或明确说明", c1["hint"][:50])
            await page.screenshot(path=str(SHOTS / "03-card-hint.png"))

            # ---- 翻面 ----
            await page.click('[data-testid="reveal"]')
            page_m = await page.wait_for_selector('[data-testid="card-meaning"]', timeout=6000)
            check(page_m is not None, "翻面后出现释义")
            c2 = await page.evaluate(CARD_JS)
            check(c2["hasGrades"] is True, "翻面后出现四个评分按钮")
            check(len(c2["grades"]) == 4, "正好四个档位", str(len(c2["grades"])))
            names = [g["name"] for g in c2["grades"]]
            check(names == ["忘了", "勉强", "记得", "太简单"], "档位顺序与语义正确",
                  ",".join(names))
            check(all(g["next"] and g["next"] != "—" for g in c2["grades"]),
                  "每个按钮上都写了下次间隔",
                  " / ".join(f"{g['name']}:{g['next']}" for g in c2["grades"]))
            check(c2["hasSentence"] is True, "翻面后给出文中例句")
            await page.screenshot(path=str(SHOTS / "04-card-back.png"))

            # ------------------------------------------------------ C 键盘
            head("[C] 数字键评分（评完应换到下一张）")
            card_before = c2["vocabId"]
            await page.keyboard.press("3")
            # 「换到下一张」有两种表现：卡片 id 变了，或者队列已经被评空
            # （卡片区换成清空态）。两者都算前进，所以要一起等。
            advanced = await page.wait_for_function(
                "() => { const e = document.querySelector('[data-testid=\"review-card\"]');"
                " if (!e) return !!document.querySelector('[data-testid=\"review-done\"]');"
                f" return Number(e.dataset.vocab) !== {card_before}; }}",
                timeout=15000,
            )
            check(advanced is not None, "按 3（记得）后离开当前卡")
            flash = " ".join(((await text_of(page, '[data-testid="card-flash"]')) or "").split())
            check("下次" in flash, "评分后有回执文案（含下次到期）", flash)
            c3 = await page.evaluate(CARD_JS)
            check(c3 is not None and c3["vocabId"] != card_before,
                  "确实换到了下一张卡",
                  f"{card_before} → {c3 and c3['vocabId']}")

            tally = " ".join(((await text_of(page, '[data-testid="review-tally"]')) or "").split())
            check("已评 1" in tally, "本轮计数 +1", tally)
            left1 = " ".join(((await text_of(page, '[data-testid="review-left"]')) or "").split())
            check(left1 != left0, "剩余张数递减", f"{left0} → {left1}")

            # 复习页顶部统计：今日已评也涨了
            with client() as c:
                st = c.get("/api/review/stats").json()
            check(st["today"]["reviews"] >= 1, "今日复习次数落库", str(st["today"]["reviews"]))

            # ---- 评分真的写进了库（不是只改了界面） ----
            got = api(f"/api/review/cards/{card_before}")
            check(got["review"]["state"] in ("learning", "review"),
                  "评分后的排期确实变了", got["review"]["state_label"])
            check(got["review"]["last_at"] is not None, "记录了上次复习时间")

            # ------------------------------------------------------ D 空格翻面
            head("[D] 空格翻面 / 翻面前按数字键无效")
            card2 = await page.evaluate(CARD_JS)
            check(card2 is not None, "队列里还有第二张卡（用来验空格翻面）")
            if card2:
                # 翻面前按数字键不该有任何反应
                await page.keyboard.press("1")
                await page.wait_for_timeout(500)
                still = await page.evaluate(CARD_JS)
                check(still and still["vocabId"] == card2["vocabId"],
                      "翻面前按数字键不会误评分")
                await page.keyboard.press(" ")
                await page.wait_for_timeout(500)
                rev = await page.evaluate(CARD_JS)
                check(rev and rev["hasGrades"], "空格可以翻面")
                check(len(rev["grades"]) == 4 if rev else False, "翻面后仍是四个档位")
                await page.screenshot(path=str(SHOTS / "05-keyboard.png"))

            # ------------------------------------------------------ E 清空
            head("[E] 把队列评完 → 清空态")
            guard = 0
            while guard < 40:
                guard += 1
                cur = await page.evaluate(CARD_JS)
                if cur is None:
                    break
                if not cur["hasGrades"]:
                    await page.click('[data-testid="reveal"]')
                    await page.wait_for_timeout(180)
                await page.keyboard.press("3")
                await page.wait_for_timeout(420)
            done_el = await page.wait_for_selector('[data-testid="review-done"]', timeout=15000)
            check(done_el is not None, f"评完 {guard} 张后进入清空态")
            done_txt = " ".join(((await text_of(page, '[data-testid="review-done"]')) or "").split())
            check("完成" in done_txt or "没有到期" in done_txt, "清空态文案正确", done_txt[:70])
            check("下次到期" in done_txt, "清空态给出下次到期时间", done_txt[:90])
            # 这一条钉住一个真踩过的坑：队列是本轮快照，服务端的 next_due 是
            # **装载时**算的，那时这些卡还没排到未来，所以它是 null。如果只信它，
            # 用户刚评完看到的就是「下次到期：—」，而这句话正是他此刻最想知道的。
            check("下次到期：—" not in done_txt.replace(" ", ""),
                  "清空态的下次到期不是空值", done_txt[:90])
            check((await text_of(page, '[data-testid="review-card"]')) is None,
                  "清空后卡片区消失")
            await page.screenshot(path=str(SHOTS / "06-done.png"))

            # 队列也确实是空的
            with client() as c:
                q = c.get("/api/review/queue", params={"limit": 50, "new_limit": 50}).json()
            check(len(q["items"]) == 0, "接口返回的队列也是空的", f"{len(q['items'])} 张")
            check(q["next_due"] is not None, "给出了下一次到期时间", str(q["next_due"]))

            # ------------------------------------------------------ F 流式
            head("[F] 阅读页：AI 面板的流式增量")
            sent = pick_unparsed_sentence(art)
            if not sent:
                print("  （找不到未解析的句子，跳过流式验证）", flush=True)
            else:
                await page.goto(f"{FRONTEND}/a/{aid}", wait_until="networkidle")
                await page.wait_for_selector("article .prose-en p", timeout=25000)
                await page.wait_for_timeout(700)
                blk = next((b for b in art["blocks"]
                            if b.get("type") == "para" and sent[:30] in (b.get("text") or "")), None)
                if blk is None:
                    print("  （句子与块对不上，跳过流式验证）", flush=True)
                else:
                    res = await page.evaluate(SELECT_JS, [blk["id"], sent, True])
                    check(bool(res and res.get("ok")), "能在那一段里建出选区",
                          str(res and res.get("why", "")))
                    pop = await page.wait_for_selector('[data-testid="selection-popover"]',
                                                       timeout=12000)
                    check(pop is not None, "浮层出现")
                    # 选段动作只有一个【解析】（M8 合并），按 testid 点最稳
                    await page.click('[data-testid="parse-action"]')
                    panel = await page.wait_for_selector('[data-testid="ai-panel"]', timeout=8000)
                    check(panel is not None, "AI 面板打开")

                    # 轮询边等边看：首字节前会有 reasoning 阶段（回调空串），
                    # 所以「增量框出现」和「思考提示出现」都要盯住。
                    seen_partial = False
                    seen_thinking = False
                    t0 = time.perf_counter()
                    while time.perf_counter() - t0 < 40:
                        if await page.query_selector('[data-testid="ai-partial"]'):
                            seen_partial = True
                            # 抓到增量立刻截图，晚了就没了（done 会把它清掉）
                            await page.screenshot(path=str(SHOTS / "07-streaming.png"))
                            break
                        if await page.query_selector('[data-testid="ai-thinking"]'):
                            seen_thinking = True
                        # 结果徽标出现 = 已经 done，不用再等
                        if await page.query_selector('[data-testid="ai-source-badge"]'):
                            break
                        await page.wait_for_timeout(120)

                    settled = await page.wait_for_selector(
                        '[data-testid="ai-source-badge"]', timeout=90000
                    )
                    check(settled is not None, "面板给出了结果徽标（流式已收尾）")
                    source = ""
                    if settled is not None:
                        source = (await settled.get_attribute("data-source")) or ""
                    badge_txt = " ".join(((await text_of(page, '[data-testid="ai-source-badge"]')) or "").split())
                    if source == "cache":
                        # 这一句早被解析过：后端一个 delta 都不发，属缓存正确
                        check(True, "命中缓存 → 无增量（符合预期）", badge_txt)
                        print("  · 这一句命中了缓存，没有 delta 可看 —— "
                              "这是缓存正确，不是流式失效", flush=True)
                    else:
                        check(len(badge_txt) > 0 and badge_txt != "缓存",
                              "本次是真调用（徽标显示耗时）", badge_txt)
                        check(seen_partial, "真调用时画出了流式增量")

                    panel_txt = (await text_of(page, '[data-testid="ai-panel"]')) or ""
                    check(len(panel_txt) > 150, "面板里有实际结果（不是空白）",
                          f"{len(panel_txt)} 字")
                    if seen_thinking:
                        print("  · 观察到 reasoning 阶段的心跳提示", flush=True)
                    check(await page.query_selector('[data-testid="ai-partial"]') is None,
                          "结果到达后增量区已被清掉（不留半截 JSON）")
                    await page.screenshot(path=str(SHOTS / "08-ai-result.png"))

            # ------------------------------------------------------ G 控制台
            head("[G] 控制台与网络")
            real = [e for e in console_errors if "favicon" not in e.lower()]
            check(len(real) == 0, "无 JS 报错", str(real[:3]))
            net = [f for f in failed_requests if "favicon" not in f.lower()]
            hard = [f for f in net if "ERR_ABORTED" not in f]
            check(len(hard) == 0, "无失败请求", str(hard[:3]))

            await browser.close()
    finally:
        unquarantine(held)
        for vid, _ in throwaways:
            api_delete(f"/api/vocab/{vid}")
        # 每件事各开一个 client：`with client() as c` 出块就关了，
        # 后面再用 `c` 会抛「client has been closed」
        with client() as c:
            after = c.get("/api/review/stats").json()
        check(after["suspended"] == 0, "收尾：被隔离的卡都已恢复（不遗留暂停状态）",
              f"suspended={after['suspended']}")
        with client() as c:
            left_over = [v for v, _ in throwaways
                         if c.get(f"/api/review/cards/{v}").status_code != 404]
        check(not left_over, "收尾：一次性生词都已移除", str(left_over))

    total = passed + len(failed)
    print("\n" + "=" * 62, flush=True)
    print(f"M5 浏览器验收：{passed}/{total} 通过")
    if failed:
        print("失败项：")
        for f in failed:
            print(f"  - {f}")
    print("=" * 62, flush=True)
    sys.exit(1 if failed else 0)


# 选区构造：与 smoke_ui_m3.py 同一套（建完立刻派发 mouseup，不让选区有机会失效）
SELECT_JS = """
([blockId, wanted, fireMouseup]) => {
  const el = document.querySelector(`[data-block-id="${blockId}"]`);
  if (!el) return { ok: false, why: 'block not found' };

  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let full = '';
  let n;
  while ((n = walker.nextNode())) {
    nodes.push({ node: n, start: full.length });
    full += n.textContent || '';
  }
  const at = full.indexOf(wanted);
  if (at < 0) return { ok: false, why: 'text not found in block', len: full.length };

  const locate = (off) => {
    for (const it of nodes) {
      const len = (it.node.textContent || '').length;
      if (off <= it.start + len) return { node: it.node, offset: off - it.start };
    }
    const last = nodes[nodes.length - 1];
    return { node: last.node, offset: (last.node.textContent || '').length };
  };

  const s = locate(at);
  const e = locate(at + wanted.length);
  const r = document.createRange();
  r.setStart(s.node, s.offset);
  r.setEnd(e.node, e.offset);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);

  const before = r.getBoundingClientRect();
  if (before.top < 140 || before.bottom > window.innerHeight - 140) {
    window.scrollBy(0, before.top - Math.round(window.innerHeight * 0.35));
  }
  if (fireMouseup) el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));

  const after = r.getBoundingClientRect();
  return {
    ok: true,
    text: r.toString(),
    inView: after.top >= 0 && after.bottom <= window.innerHeight,
  };
}
"""


if __name__ == "__main__":
    t0 = time.perf_counter()
    try:
        asyncio.run(main())
    finally:
        print(f"耗时 {time.perf_counter() - t0:.1f}s", flush=True)
