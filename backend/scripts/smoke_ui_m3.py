"""M3 浏览器验收：在真实浏览器里划词，验证「划词即落库 + 高亮区间重建」。

跑法（后端 8010 + 前端 5173 都要在跑）：
    python -u scripts/smoke_ui_m3.py
    python -u scripts/smoke_ui_m3.py --article-id 2 --headed --keep-marks

两个 M3 专属的坑，脚本里都绕开了：
  1. **已有高亮会把文本切成多个节点**，所以选区不能靠「在单个文本节点里 indexOf」，
     必须先把整块的文本节点拼起来定位，再映射回具体节点（见 SELECT_JS）。
  2. **建完选区再单独派发 mouseup 会丢选区**（中间那次 CDP 往返会让选区失效），
     必须同一个 evaluate 里建完立刻 dispatch。
  3. **浮层是 fixed 定位，位置取选区的视口坐标**，所以选区必须先在视口内才算数。
     只查「元素是否存在」会被骗：视口外的浮层 Playwright 照样算「可见」，
     然后按钮全点不动。见 SELECT_JS 的滚动收敛与「浮层落在视口内」这条断言。

**看结果时注意冷/热缓存**：`ai_logs` 的内容哈希缓存命中时，整轮只要十几秒，
这是正常且期望的（第二次划同样的词不该再烧 token）。要看真实生成路径与耗时，
换一篇没测过的文章 (`--article-id N`) 或换个测试词再跑。

最关键的一项是 D：**刷新之后，高亮的字符区间必须与后端记录逐字一致**。
这是 design 红线 2（偏移只锚定 blocks.text、高亮由区间重建）的端到端证明 ——
如果实现里偷懒把高亮写进了 HTML，这一项必然失败。

[A2] / [A3] / [C2] 三节是照着用户的原话加的回归锚点：
  [A2] 只划中词里几个字母（`Chang` → `a`）时，不能被当成一个独立单词去查
       —— 区间要吸附到整词，卡片上还要说明「已按整词查」；
  [A3] 关浮层（点 ✕ / 点空白处）之后，**浏览器的原生选区也必须一起收掉**，
       否则那行字看起来还是「选中」状态，只有重新划一次才会消失；
  [C3] 关掉右侧面板之后，正文里那圈「正在查看」的蓝框（`mark[data-active]`）
       必须收掉 —— 用户看到的就是「面板关了它还一直是选中状态」；
  [C2] 单词与短语/句子是两档底色：解析过一条长句后再划句里的词，
       得能一眼看出哪个词查过译（嵌套时内层必须赢）。
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
SHOTS = Path(__file__).resolve().parents[1] / "data" / "m3_shots"

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


# ---------------------------------------------------------------- HTTP 工具

def pick_article(want: int | None) -> dict:
    with httpx.Client(base_url=BACKEND, timeout=60) as c:
        if want is not None:
            return c.get(f"/api/articles/{want}").json()
        items = c.get("/api/articles").json()["items"]
        done = [a for a in items if a["status"] == "done" and a["block_count"] > 8]
        if not done:
            print("没有可用文章")
            sys.exit(2)
        done.sort(key=lambda a: -a["block_count"])
        return c.get(f"/api/articles/{done[0]['id']}").json()


def api_highlights(article_id: int) -> list[dict]:
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        return c.get(f"/api/articles/{article_id}/highlights").json()["items"]


def api_clear(article_id: int) -> int:
    """清空该篇痕迹，保证脚本可重复运行。"""
    n = 0
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        for h in c.get(f"/api/articles/{article_id}/highlights").json()["items"]:
            c.delete(f"/api/highlights/{h['id']}")
            n += 1
    return n


def api_delete_highlight(highlight_id: int) -> None:
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        c.delete(f"/api/highlights/{highlight_id}")


def api_logs(limit: int = 200) -> dict:
    """全局解析流水（右侧面板底部那张列表的数据源）。"""
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        return c.get("/api/ai/logs", params={"limit": limit}).json()


# ---------------------------------------------------------------- 选区工具

# 建选区 + 立刻派发 mouseup（同一个 evaluate，不让选区有机会失效）。
# 文本按「整块拼接」定位，这样块里已有 <mark> 把一个句子切成多个文本节点也能选。
#
# 关于滚动（踩过的坑）：早先只处理了「目标在视口上方」（rect.top < 120）的情况，
# 目标在下方时完全不滚。而浮层是 position: fixed，位置直接取选区的视口坐标 ——
# 于是浮层被渲染到视口外（实测 rect.y=2817，视口高才 920），Playwright 的
# wait_for_selector 只查 CSS 可见性、不查是否在视口内，所以「浮层出现 ✓」照样绿，
# 然后三个按钮全点不动。现在改成上下都收敛，并且 mouseup 前就滚好，
# 让 handleMouseUp 取到的是滚动之后的真实坐标。
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

  // 把选区滚到视口里（上下都要管），再派发 mouseup
  const before = r.getBoundingClientRect();
  if (before.top < 140 || before.bottom > window.innerHeight - 140) {
    window.scrollBy(0, before.top - Math.round(window.innerHeight * 0.35));
  }

  if (fireMouseup) el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));

  const after = r.getBoundingClientRect();
  return {
    ok: true,
    text: r.toString(),
    x: after.left,
    y: after.top,
    inView: after.top >= 0 && after.bottom <= window.innerHeight,
  };
}
"""

# 读取正文里所有高亮，连同它相对所属块的字符偏移 —— 用来和数据库记录逐字比对
READ_MARKS_JS = """
() => [...document.querySelectorAll('article mark')].map((m) => {
  const blk = m.closest('[data-block-id]');
  const r = document.createRange();
  r.setStart(blk, 0);
  r.setEndBefore(m);
  return {
    hlId: Number(m.dataset.hlId),
    text: m.textContent,
    blockId: Number(blk.dataset.blockId),
    offset: r.toString().length,
    kind: m.dataset.kind,
    bg: getComputedStyle(m).backgroundColor,
  };
})
"""

# 关浮层之后，「那行字还是不是选中的」只能靠浏览器自己的选区状态来判 ——
# 这正是用户报的第 2 个问题：浮层关了，原生选区还赖着不走。
CLOSE_PROBE_JS = """
() => {
  const s = window.getSelection();
  return {
    pop: !!document.querySelector('[data-testid="selection-popover"]'),
    ranges: s ? s.rangeCount : -1,
    collapsed: s ? s.isCollapsed : null,
    text: s ? s.toString() : '',
  };
}
"""

POP = '[data-testid="selection-popover"]'
CLOSE_BTN = '[data-testid="popover-close"]'


async def wait_for(page, selector: str, timeout: int) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--keep-marks", action="store_true", help="不清理已有痕迹")
    args = ap.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    # 先把上一轮的失败截图清掉：否则修好之后再跑，目录里会同时留着
    # `xx-fail.png` 和 `xx.png`，回看时根本分不清哪张是当前状态。
    for stale in SHOTS.glob("*fail*.png"):
        stale.unlink(missing_ok=True)
    art = pick_article(args.article_id)
    aid = art["id"]

    if not args.keep_marks:
        n = api_clear(aid)
        print(f"  （已清理该篇 {n} 条旧痕迹）", flush=True)

    para = max(
        (b for b in art["blocks"] if b["type"] == "para" and len(b.get("text") or "") > 200),
        key=lambda b: len(b["text"]),
        default=None,
    )
    if para is None:
        print("没有足够长的段落块")
        sys.exit(2)

    text = para["text"]
    m = re.search(r"\b[A-Za-z]{8,}\b", text)
    if not m:
        print("段落里没有合适的长词")
        sys.exit(2)
    word = m.group(0)
    word_off = m.start()

    # [A2] 用的第二个词：要划的是它**最后 4 个字母**（半截词）。
    # 片段必须在整块里唯一 —— SELECT_JS 靠 `indexOf` 定位，撞车就会选到别处，
    # 那样断言失败的原因会指向错误的地方。
    frag_pick = next(
        (
            (mm.group(0), mm.start(), mm.group(0)[-4:])
            for mm in re.finditer(r"\b[A-Za-z]{8,}\b", text)
            if mm.group(0) != word and text.count(mm.group(0)[-4:]) == 1
        ),
        None,
    )

    console_errors: list[str] = []
    failed_requests: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 920})
        page = await ctx.new_page()
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on(
            "requestfailed",
            lambda req: failed_requests.append(f"{req.method} {req.url} {req.failure}")
            if "favicon" not in req.url
            else None,
        )

        await page.goto(f"{FRONTEND}/a/{aid}", wait_until="networkidle")
        await page.wait_for_selector("article .prose-en p", timeout=25000)
        print(f"  样本：#{aid} {art['title'][:40]!r}")
        print(f"  测试词：{word!r}（块 {para['id']}，块内偏移 {word_off}）", flush=True)

        # ---------------------------------------------------------- A 划词
        head("[A] 划词：落库 + 浮层 + 音标 + 释义")
        before = len(api_highlights(aid))
        res = await page.evaluate(SELECT_JS, [para["id"], word, True])
        if not check(bool(res.get("ok")), "在正文里建立选区并触发划词",
                     str(res.get("why", ""))):
            await browser.close()
            return

        if await wait_for(page, POP, 10000):
            check(True, "划词浮层出现")
            # 浮层是 fixed 定位，可能落在视口外 —— 那时 Playwright 仍然算它
            # 「可见」，后面点不到却报不出原因，所以这里先卡一道。
            inview = await page.evaluate(
                "() => { const p = document.querySelector('" + POP + "');"
                " if (!p) return null; const r = p.getBoundingClientRect();"
                " return { top: Math.round(r.top), bottom: Math.round(r.bottom),"
                " vh: innerHeight, ok: r.top >= 0 && r.bottom <= innerHeight }; }"
            )
            check(
                bool(inview and inview["ok"]),
                "浮层落在视口内（能真的点到）",
                str(inview),
            )
        else:
            check(False, "划词浮层出现", "超时")

        # 音标来自词典，应该很快。
        # 注意断言要卡在 data-testid="phonetic-line" 上：早先版本用
        # `textContent.includes('音标')` 判过，结果被「暂无音标」这四个字里的
        # 「音标」骗过去，明明没有音标也算通过 —— 假绿灯比红灯更坏。
        try:
            await page.wait_for_selector(f'{POP} [data-testid="phonetic-line"]', timeout=30000)
            ipa = await page.get_attribute(f'{POP} [data-testid="phonetic-line"]', "data-phonetic")
            src = await page.get_attribute(f'{POP} [data-testid="phonetic-line"]', "data-source")
            check(
                bool(ipa) and ipa.startswith("/") and len(ipa) > 2,
                "音标已显示（真 IPA，非占位文案）",
                f"{ipa}  source={src}",
            )
        except Exception:
            pop_text = await page.text_content(POP) or ""
            missing = await page.query_selector(f'{POP} [data-testid="phonetic-missing"]')
            check(
                False,
                "音标已显示（真 IPA，非占位文案）",
                ("词卡显示未收录： " if missing else "没等到音标栏： ")
                + " ".join(pop_text.split())[:90],
            )

        # ---- 发音按钮（M7 重做：有道优先 + 一律本地化）
        # 早先版本只按文件名前缀判口音，Lingua Libre 的条目全被丢掉，
        # 于是「词典明明有录音，界面上却没有喇叭」。
        #
        # 现在只断言一个方向（端点说有 → 界面必须有）。反方向**不能**断言：
        # 变形词的词条常自己没有录音（`invading`），而词卡会正确地回退到
        # 原形（`invade`）的录音 —— 那是更对的行为，别把它判成回归。
        ph = httpx.get(f"{BACKEND}/api/dict/{word}", timeout=25).json()
        # M10：一个词有英美两个槽（都没下过时是 pending，不是没有）。
        # 只要**有一个**不是 missing，界面上就该出现喇叭 —— 点哪个下哪个。
        slots = ph.get("audios") or {}
        want_audio = any((s or {}).get("status") != "missing" for s in slots.values())
        btn = await page.query_selector(f'{POP} [data-testid="play-audio"]')
        check(
            (not want_audio) or bool(btn),
            "端点有发音则界面必须有喇叭（M7 回归点）",
            f"{word}: 端点={'有' if want_audio else '无'} 界面={'有' if btn else '无'}",
        )
        if btn:
            accent = await page.get_attribute(f'{POP} [data-testid="play-audio"]', "data-accent")
            url = await page.get_attribute(f'{POP} [data-testid="play-audio"]', "data-url") or ""
            check(
                accent in ("us", "uk", "other"),
                "data-accent 是原值而非展示文案",
                f"accent={accent}",
            )
            # 这一条是 M7 的核心：界面**不许**出现外链。
            # 音频必须先落到 data/audio/，再由 /api/dict/{word}/audio 吐出来。
            check(
                url.startswith("/api/dict/") and "://" not in url,
                "喇叭指向本机端点，不是有道/Commons 的外链（音频已本地化）",
                url,
            )
            if url.startswith("/api/dict/"):
                got = httpx.get(f"{BACKEND}{url}", timeout=60)
                check(
                    got.status_code == 200
                    and (got.headers.get("content-type") or "").startswith("audio/"),
                    "喇叭指向的地址真能取到音频",
                    f"HTTP {got.status_code} {got.headers.get('content-type')} {len(got.content)} B",
                )
        else:
            check(True, "data-accent 是原值而非展示文案", "无按钮，跳过")
            check(True, "喇叭指向本机端点，不是有道/Commons 的外链（音频已本地化）", "无按钮，跳过")

        # 释义要等模型 —— 直接等词卡里那个释义节点，不再用模糊正则
        try:
            await page.wait_for_selector(f'{POP} [data-testid="dict-meaning"]', timeout=90000)
            meaning = " ".join(
                ((await page.text_content(f'{POP} [data-testid="dict-meaning"]')) or "").split()
            )
            check(bool(meaning), "释义已返回", meaning[:90])
            await page.screenshot(path=str(SHOTS / "01-word-card.png"))
        except Exception:
            pop_text = await page.text_content(POP) or ""
            check(False, "释义已返回", " ".join(pop_text.split())[:150])
            await page.screenshot(path=str(SHOTS / "01-word-card-fail.png"))

        await page.wait_for_timeout(600)
        marks = await page.evaluate(READ_MARKS_JS)
        target = next((x for x in marks if x["text"] == word), None)
        check(target is not None, f"正文出现高亮 {word!r}", f"共 {len(marks)} 处")
        if target:
            check(
                target["blockId"] == para["id"] and target["offset"] == word_off,
                "高亮区间与选区一致",
                f"block={target['blockId']} off={target['offset']} vs {word_off}",
            )

        after = api_highlights(aid)
        check(len(after) > before, "划词已落库（需求 5）", f"{before} → {len(after)}")
        stored = next(
            (h for h in after if h["block_id"] == para["id"] and h["start_offset"] == word_off),
            None,
        )
        check(stored is not None, "库内记录与选区对齐",
              f"{stored and (stored['start_offset'], stored['end_offset'], stored['kind'])}")
        hid = stored["id"] if stored else None

        # ---------------------------------------------------------- A2 只划中半截词
        # 用户报的：拖选时手一抖只圈住词里几个字母（`Chang` → `a`），
        # 那半截字母会被当成一个独立单词去查词典、进生词本，查回来全是胡说。
        # 现在要求「划词只认整词」：落在词内的片段被吸附到整个词。
        head("[A2] 划中词内片段 → 吸附到整词（划词只认整词）")
        if not frag_pick:
            check(False, "找到可测的半截词片段", "段落里没有合适的长词")
        else:
            fword, foff, frag = frag_pick
            r2 = await page.evaluate(SELECT_JS, [para["id"], frag, True])
            check(bool(r2.get("ok")), f"只选中 {fword!r} 里的 {frag!r}",
                  f"{r2.get('why','')} 选中={r2.get('text')!r}")
            if await wait_for(page, POP, 8000):
                kind = await page.get_attribute(POP, "data-kind")
                head_text = " ".join(((await page.text_content(POP)) or "").split())
                check(kind == "word", "吸附后当成一个词（走词卡而不是解析）", f"kind={kind}")
                note = await page.query_selector(f'{POP} [data-testid="snapped-note"]')
                check(note is not None, "词卡上说明了「已按整词查」", head_text[:80])
                check(fword in head_text,
                      "卡片里给的是整个词，不是那半截字母", head_text[:80])
            else:
                check(False, "半截词选区也弹了浮层", "超时")

            fhits = api_highlights(aid)
            fstored = next(
                (h for h in fhits
                 if h["block_id"] == para["id"] and h["start_offset"] == foff),
                None,
            )
            check(fstored is not None
                  and fstored["end_offset"] == foff + len(fword)
                  and fstored["kind"] == "word",
                  "落库区间是整个词（不是那半截）",
                  f"{fstored and (fstored['start_offset'], fstored['end_offset'], fstored['kind'])}"
                  f" vs {foff}~{foff + len(fword)}")
            await page.wait_for_timeout(500)
            fmarks = await page.evaluate(READ_MARKS_JS)
            fmark = next((x for x in fmarks if x["text"] == fword), None)
            check(fmark is not None and fmark["offset"] == foff and fmark["kind"] == "word",
                  "正文里高亮的是整个词",
                  f"{fmark and (fmark['text'], fmark['offset'], fmark['kind'])}")

            # ------------------------------------------------------ A3 关浮层要收选区
            # 用户报的：点空白处 / 点 ✕ 之后，那行字看起来还是「选中」状态 ——
            # 因为是浏览器的原生选区没被收掉（浮层关掉不等于选区消失）。
            head("[A3] 关浮层之后原生选区必须一起收掉")
            await page.click(CLOSE_BTN, timeout=5000)
            await page.wait_for_timeout(200)
            st = await page.evaluate(CLOSE_PROBE_JS)
            check(not st["pop"], "点 ✕ 浮层关闭")
            check(st["ranges"] == 0, "点 ✕ 之后浏览器选区也被清掉（这是用户报的那条）",
                  f"ranges={st['ranges']} 残留={st['text'][:40]!r}")

            r3 = await page.evaluate(SELECT_JS, [para["id"], word, True])
            check(bool(r3.get("ok")), "再划一次（为点空白处的场景准备）", r3.get("why", ""))
            if await wait_for(page, POP, 8000):
                # 点在正文左侧的空白处：既不在浮层里，也不在任何文字上
                await page.mouse.click(24, 500)
                await page.wait_for_timeout(250)
                st = await page.evaluate(CLOSE_PROBE_JS)
                check(not st["pop"], "点空白处浮层关闭")
                # 这里**不能**要求 rangeCount == 0：点空白处时浏览器自己的默认动作
                # 会在正文里落一个「空选区」（collapsed，选中的文字为空）——
                # 那画不出任何选中框，正是我们要的结果。断言要看「还有没有选中文字」。
                check(st["ranges"] == 0 or (st["collapsed"] is True and not st["text"]),
                      "点空白处之后选中框消失（没有残留的选中文字）",
                      f"ranges={st['ranges']} collapsed={st['collapsed']} "
                      f"残留={st['text'][:40]!r}")
            else:
                check(False, "再划一次后浮层出现", "超时")

        # ---------------------------------------------------------- B 刷新后重建
        head("[B] 刷新后：高亮位置逐字正确（红线 2 的核心验证）")
        await page.reload(wait_until="networkidle")
        await page.wait_for_selector("article .prose-en p", timeout=25000)
        await page.wait_for_timeout(1600)

        marks2 = await page.evaluate(READ_MARKS_JS)
        t2 = next((x for x in marks2 if x["hlId"] == hid), None)
        check(t2 is not None, "刷新后高亮仍在", f"共 {len(marks2)} 处")
        if t2 and stored:
            check(
                t2["text"] == stored["surface"]
                and t2["offset"] == stored["start_offset"]
                and t2["blockId"] == stored["block_id"],
                "区间与后端记录逐字一致（高亮由区间重建）",
                f"{t2['text']!r} @{t2['offset']} vs 库 {stored['start_offset']}",
            )
        await page.screenshot(path=str(SHOTS / "02-highlight-restored.png"))

        # ---------------------------------------------------------- B2 发音（M7）
        # 历史：用户报「词卡上没有喇叭」，根因是 Wiktionary 的 `{{audio}}` 在新式
        # Lingua Libre 文件里把口音写在具名参数 `a=Southern England`，旧解析器
        # 只看文件名前缀，判不出就整条丢掉。
        #
        # 现在发音已经**换源到有道**（美音）并把音频本地化，所以这一节验的是
        # 新链路：划词 → 词卡出喇叭 → 喇叭指向本机端点 → 那个端点真吐音频。
        head("[B2] 发音音频（M7：有道源 + 本地化，界面不许出现外链）")
        dblock = dword = doff = None
        for b in art["blocks"]:
            t = b.get("text") or ""
            i = t.find("documentary")
            if i >= 0 and (i == 0 or not t[i - 1].isalpha()):
                dblock, dword, doff = b["id"], "documentary", i
                break
        if dblock is None:
            print("  （本篇没有 documentary，跳过 —— 后端用例已在 smoke_ai [B2] 覆盖）")
        else:
            r2 = await page.evaluate(SELECT_JS, [dblock, dword, True])
            check(bool(r2.get("ok")), f"建立选区 {dword!r}",
                  str(r2.get("why", "")))
            if await wait_for(page, f'{POP} [data-testid="play-audio"]', 30000):
                acc = await page.get_attribute(
                    f'{POP} [data-testid="play-audio"]', "data-accent"
                )
                url = await page.get_attribute(
                    f'{POP} [data-testid="play-audio"]', "data-url"
                ) or ""
                check(
                    acc == "us",
                    "documentary 的喇叭出现且标为美式",
                    f"data-accent={acc}",
                )
                check(
                    url.startswith("/api/dict/") and "://" not in url,
                    "喇叭指向本机端点（音频已下载到本地）",
                    url,
                )
                if url.startswith("/api/dict/"):
                    got = httpx.get(f"{BACKEND}{url}", timeout=60)
                    check(
                        got.status_code == 200 and got.content[:3] == b"ID3",
                        "本机端点吐出的是真 mp3（有道的 ID3 头）",
                        f"HTTP {got.status_code} {len(got.content)} B head={got.content[:3]!r}",
                    )
                await page.screenshot(path=str(SHOTS / "02b-audio-button.png"))
                # 再单独截浮层：整页截图里词卡常被右侧面板压住，
                # 回看时看不出喇叭长什么样。
                await page.locator(POP).screenshot(
                    path=str(SHOTS / "02c-audio-button-card.png")
                )
            else:
                pop = await page.text_content(POP) or ""
                check(False, "documentary 的喇叭出现且标为美式",
                      "没等到 play-audio：" + " ".join(pop.split())[:100])
            # 这次划的痕迹留给下一轮的 api_clear 收拾，不在这里删 ——
            # 删了会把 [C] 的痕迹计数搅乱。

        # ---------------------------------------------------------- C 选段解析
        head("[C] 选段 → 解析 → 右侧面板")
        # 从同一段里挑一个完整句子（避开刚划过的那句，减少混淆）
        sent = None
        sent_off = 0
        for mm in re.finditer(r"[A-Z][^.!?]{40,190}[.!?]", text):
            if not (mm.start() <= word_off < mm.end()):
                sent = mm.group(0)
                sent_off = mm.start()
                break
        if sent is None:
            check(False, "找到测试用句子", "段落里没有合适的长句")
        else:
            r2 = await page.evaluate(SELECT_JS, [para["id"], sent, True])
            check(bool(r2.get("ok")), "建立句子选区并触发", f"{len(sent)} 字 {r2.get('why','')}")
            if await wait_for(page, POP, 10000):
                kind = await page.get_attribute(POP, "data-kind")
                check(kind in ("sentence", "phrase", "paragraph"), "识别为选段", f"kind={kind}")

                # 选段不自动调模型，只给一个【解析】按钮。
                # 这里一定要把失败原因记下来：早先版本 except 里只 continue，
                # 按钮点不动也只报一句「点按钮失败」，完全没法查。
                clicked = False
                last_err = "（没找到【解析】按钮）"
                for sel in ('[data-testid="parse-action"]', f"{POP} >> text=解析"):
                    try:
                        await page.click(sel, timeout=4000)
                        clicked = True
                        break
                    except Exception as e:  # noqa: BLE001
                        last_err = f"{sel}: {type(e).__name__}: {str(e).splitlines()[0][:120]}"
                if not clicked:
                    alive = await page.evaluate(
                        "() => { const p = document.querySelector('" + POP + "');"
                        " if (!p) return 'popover 已不在 DOM';"
                        " const r = p.getBoundingClientRect();"
                        " const cs = getComputedStyle(p);"
                        " return `rect=${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.width)}x${Math.round(r.height)}"
                        " vis=${cs.visibility} disp=${cs.display} vh=${innerHeight} scrollY=${Math.round(window.scrollY)}`; }"
                    )
                    last_err = f"{last_err} | 点击后浮层状态: {alive}"
                    await page.screenshot(path=str(SHOTS / "03-click-fail.png"))
                check(clicked, "点动作按钮", "" if clicked else last_err)

                if await wait_for(page, '[data-testid="ai-panel"]', 8000):
                    check(True, "AI 面板打开")
                else:
                    check(False, "AI 面板打开", "超时")

                try:
                    # 新版解析的固定锚点是裸的 `parse-result`（旧版解析是 `parse-legacy`）。
                    # 不要拿「译文」二字当锚：等待中面板本来就会显示阶段文案「生成译文…」，
                    # 那一命中就等于没等。
                    await page.wait_for_selector('[data-testid="parse-result"]', timeout=120000)
                    panel_text = " ".join((await page.text_content('[data-testid="ai-panel"]') or "").split())
                    check(True, "解析结果已渲染", panel_text[:140])
                    await page.screenshot(path=str(SHOTS / "03-ai-explain.png"))
                except Exception:
                    panel_text = " ".join((await page.text_content('[data-testid="ai-panel"]') or "").split())
                    check(False, "解析结果已渲染", panel_text[:140] or "面板为空")
                    await page.screenshot(path=str(SHOTS / "03-ai-explain-fail.png"))

                logs = await page.evaluate(
                    "() => [...document.querySelectorAll('.er-log-item')].map(b => b.textContent)"
                )
                check(len(logs) >= 1, "解析记录进入历史列表", f"{len(logs)} 条")

                # ------------------------------------------------------ C3 蓝框要收掉
                # 用户报的：解析完（正文里那条被套上蓝框 = 「正在看这条」）之后
                # 点右侧面板的 ✕ 关掉面板，蓝框还留在正文里，看起来一直是选中状态。
                head("[C3] 关面板 / 点空白 → 正文里的「正在查看」蓝框收掉")
                ACTIVE_JS = (
                    "() => document.querySelectorAll('article mark[data-active=\"1\"]').length"
                )
                # 蓝框要等历史列表 refetch 落地（activeLogId 命中 logs 里的那一条
                # 才会挂 data-active），所以这里轮询等，别用一次性快照判死。
                active = 0
                for _ in range(15):
                    active = await page.evaluate(ACTIVE_JS)
                    if active >= 1:
                        break
                    await page.wait_for_timeout(200)
                check(active >= 1, "解析中的那条痕迹带蓝框（看得出现在在看哪条）",
                      f"{active} 处")
                clicked = False
                for sel in ('[data-testid="ai-panel"] button[title="关闭面板"]',
                            '[data-testid="ai-panel"] button:has-text("✕")'):
                    try:
                        await page.click(sel, timeout=4000)
                        clicked = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
                check(clicked, "关掉右侧面板")
                await page.wait_for_timeout(300)
                check(await page.evaluate(ACTIVE_JS) == 0,
                      "关面板后蓝框消失（不再显示成选中状态）",
                      f"{await page.evaluate(ACTIVE_JS)} 处")
            else:
                check(False, "选段浮层出现", "超时")

        # ---------------------------------------------------------- C2 两档底色
        # 用户报的：解析过一条长句之后再划句里的词，两者底色一样，
        # 分不清哪个词查过译。修法 = 词/句两档底色 + 嵌套时内层（词）赢。
        head("[C2] 单词与短语/句子两档底色，嵌套时词看得见自己的颜色")
        nest_sent = next(
            (
                mm.group(0)
                for mm in re.finditer(r"[A-Z][^.!?]{40,190}[.!?]", text)
                if mm.start() <= word_off < mm.end()
            ),
            # 兜底：这一段本身可能就是一整句超长句（或词正好落在句首）。
            # 兜底取一个**一定包住这个词**的长窗口 —— 这一节验的是「长选区里
            # 套着的词看得见自己的颜色」，不依赖句子的语法完整性。
            text[: min(len(text), word_off + len(word) + 120)],
        )
        if len(nest_sent) <= len(word):
            check(False, "找到包含测试词的长选区", f"只有 {len(nest_sent)} 字")
        else:
            r4 = await page.evaluate(SELECT_JS, [para["id"], nest_sent, True])
            check(bool(r4.get("ok")), "划中「包含那个词的长选区」并触发",
                  f"{len(nest_sent)} 字 {r4.get('why','')}")
            # 划中**不落痕**：这条长高亮要点了【解析】才存在（没点过就当没划过）。
            # 点它顺带把浮层收掉（runAi 会 closePopover），别让它压住截图。
            parsed_click = False
            for sel in ('[data-testid="parse-action"]', f"{POP} >> text=解析"):
                try:
                    await page.click(sel, timeout=4000)
                    parsed_click = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            check(parsed_click, "长选区点【解析】——高亮在这一刻才落库",
                  f"{len(nest_sent)} 字")

            # 落痕是异步的（POST 回来才 setAdded）；两条高亮都在了再断言底色
            nest = await page.evaluate(READ_MARKS_JS)
            for _ in range(25):
                if (any(x["text"] == word for x in nest)
                        and any(x["kind"] in ("sentence", "phrase", "paragraph")
                                for x in nest)):
                    break
                await page.wait_for_timeout(250)
                nest = await page.evaluate(READ_MARKS_JS)
            inner = next((x for x in nest if x["text"] == word), None)
            outer = next(
                (x for x in nest if x["kind"] in ("sentence", "phrase", "paragraph")),
                None,
            )
            check(inner is not None,
                  "长句没有把里面的词吃掉（词仍是独立的一条 <mark>）",
                  f"{word!r} / 共 {len(nest)} 处高亮")
            check(outer is not None, "长句自己也渲染成高亮", f"kind={outer and outer['kind']}")
            if inner and outer:
                check(inner["bg"] != outer["bg"],
                      "词与句的底色不同（这才看得出哪个词查过译）",
                      f"词={inner['bg']} 句={outer['bg']}")
            check(await page.query_selector("mark[data-kind='word']") is not None,
                  "mark 带 data-kind，样式按类别分流")
            await page.screenshot(path=str(SHOTS / "03b-nested-colors.png"))

        # ---------------------------------------------------------- D 主题
        head("[D] 主题切换后高亮仍可见")
        await page.evaluate(
            "() => { const b = [...document.querySelectorAll('.er-swatch')]"
            ".find(x => x.dataset.themeSwatch === 'dark'); if (b) b.click(); }"
        )
        await page.wait_for_timeout(700)
        mark_bg = await page.evaluate(
            "() => { const m = document.querySelector('article mark');"
            " return m ? getComputedStyle(m).backgroundColor : null; }"
        )
        check(mark_bg is not None and "0, 0, 0, 0" not in str(mark_bg),
              "深色主题下高亮有色", str(mark_bg))
        dark = await page.evaluate(READ_MARKS_JS)
        dword = next((x for x in dark if x["kind"] == "word"), None)
        dsent = next((x for x in dark if x["kind"] in ("sentence", "phrase", "paragraph")), None)
        check(
            bool(dword and dsent and dword["bg"] != dsent["bg"]),
            "深色主题下两档底色仍然分得开",
            f"词={dword and dword['bg']} 句={dsent and dsent['bg']}",
        )
        await page.screenshot(path=str(SHOTS / "04-dark-highlight.png"))

        # ---------------------------------------------------------- E 删除高亮
        head("[E] 删除高亮")
        if hid:
            n_before = len(await page.evaluate(READ_MARKS_JS))
            api_delete_highlight(hid)
            await page.reload(wait_until="networkidle")
            await page.wait_for_selector("article .prose-en p", timeout=25000)
            await page.wait_for_timeout(1400)
            n_after = len(await page.evaluate(READ_MARKS_JS))
            check(n_after < n_before, "删除后高亮消失", f"{n_before} → {n_after}")

        # ---------------------------------------------------------- F 删解析记录
        # 划词是「选中即翻译」，手一抖就会多一条记录 —— 面板底部每条都要能删。
        # 删最新的那条（就是本次运行刚产生的），不碰用户的历史。
        head("[F] 删除解析记录（误操作清理）")
        before_logs = api_logs()
        newest_id = max((l["id"] for l in before_logs["items"]), default=0)
        if not await page.query_selector('[data-testid="ai-panel"]'):
            try:
                await page.click('button:has-text("打开 AI 面板")', timeout=5000)
            except Exception:  # noqa: BLE001
                pass
        try:
            await page.wait_for_selector('[data-testid="ai-panel"] .er-log-item', timeout=8000)
            # 按 data-log-id 精确定位：记录项里的「#39」与下一行正文直接相连，
            # 拿文本子串（或 `#39\b`）去找都会踩到「9 与汉字之间没有词边界」这个坑
            row = page.locator(f'.er-del-host[data-log-id="{newest_id}"]')
            check(await row.count() == 1, "最新那条记录在列表里", f"#{newest_id}")
            if await row.count() == 1:
                # 先点开它，让这条变成「正在查看」的那条 —— 要验的正是这个场景：
                # 删掉面板里开着的那条，面板要退回空态但不关掉（用户可能还在
                # 对比别的记录，顺手关掉面板太粗暴）
                await row.locator(".er-log-item").click()
                await page.wait_for_timeout(500)
                await row.hover()
                btn = row.locator('[data-testid="ai-log-delete"]')
                check(await btn.count() == 1, "记录带删除按钮")
                await btn.click()
                await page.wait_for_timeout(700)
                left = await page.locator(".er-del-host").count()
                check(
                    await page.locator(f'.er-del-host[data-log-id="{newest_id}"]').count() == 0,
                    "点删除后该条从列表消失",
                    f"列表剩 {left} 条",
                )
                check(await page.locator('[data-testid="ai-panel"]').count() == 1,
                      "面板没有被顺手关掉")
                panel_txt = " ".join(
                    (await page.text_content('[data-testid="ai-panel"]') or "").split()
                )
                check("选一条查看" in panel_txt, "面板退回空态（内容已失去凭据）",
                      panel_txt[:60])
                after = api_logs()
                check(
                    after["total"] == before_logs["total"] - 1,
                    "后端同步删除",
                    f"{before_logs['total']} → {after['total']}",
                )
        except Exception as e:  # noqa: BLE001
            check(False, "面板底部记录列表可用", str(e)[:120])

        # ---------------------------------------------------------- G 控制台
        head("[G] 控制台与网络")
        real = [e for e in console_errors if "favicon" not in e.lower()]
        check(len(real) == 0, "无 JS 报错", str(real[:2]))
        net = [f for f in failed_requests if "favicon" not in f.lower()]
        # Chromium 会把「204 No Content 的 DELETE」额外记一条 net::ERR_ABORTED，
        # 而 204 本身已经完整收到 —— 页面侧 fetch 拿到的是 resolve(204)、后端也
        # 确实删掉了（[F] 段的四条断言就是按这个判定通过的，单独用 diag 脚本在
        # 浏览器里读过 fetch 的返回值）。这是记账行为不是真失败，摘出去但照实打印。
        noise = [f for f in net if f.startswith("DELETE ") and "ERR_ABORTED" in f]
        hard = [f for f in net if f not in noise]
        check(len(hard) == 0, "无失败请求", str(hard[:2]))
        if noise:
            print(f"    （已知噪声 {len(noise)} 条：204 DELETE 的 ERR_ABORTED）", flush=True)

        await browser.close()

    total = passed + len(failed)
    print("\n" + "=" * 62, flush=True)
    print(f"M3 浏览器验收：{passed}/{total} 通过")
    if failed:
        print("失败项：")
        for f in failed:
            print(f"  - {f}")
    print("=" * 62, flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    t0 = time.perf_counter()
    try:
        asyncio.run(main())
    finally:
        print(f"耗时 {time.perf_counter() - t0:.1f}s", flush=True)
