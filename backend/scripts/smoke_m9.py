"""M9 浏览器验收：标题划词/划句 + 右栏拖动改宽。

跑法（后端 8010 要在跑；前端默认打 Vite 5173，也可打卡 8010 的构建产物）：
    python -u scripts/smoke_m9.py
    python -u scripts/smoke_m9.py --article-id 1 --headed
    FRONT_URL=http://127.0.0.1:8010 python -u scripts/smoke_m9.py

验的是两件事：

  A/B/C/D —— **标题就是正文里的一个块**。它原本只存在 `articles.title` 里，
  划不了词；现在入库时会写成 `blocks` 的一行（`type='title'`，见
  `services/ingest.title_block`），于是划词/落库/高亮重建/深链定位四条路径与正文
  完全同构。所以这里不验「标题有没有高亮」这种表面现象，而是逐条对账：
  DOM 里的块偏移 == 库里的 `start_offset` == 标题里的真实位置，刷新后按区间重建
  （嵌套时段切分正确、词/句两档底色）。

  E —— **右栏左边缘可拖**。默认 380、命中区跨在边框上、悬停点亮、拖动改宽、
  刷新保持、双击复位，宽度落在 localStorage 而不是后端。

两条**不污染用户数据**的刻意做法：

  1. 划词那一节把 `/api/dict/` 的请求**掐断**。要验的是「标题能不能像正文一样划、
     能不能落库」，不需要真查词典 —— 更不能顺手把测试词塞进用户的生词本
     （收录是划词时自动发生的）。词卡显示为错误态是预期。
     划句那一节只验浮层（**不落库**：没点【解析】就当没划过），后面要用的
     短语痕迹直接走接口造一条，所以整条链依然不调模型、不烧 token。
  2. 跑完删掉自己造的痕迹，并断言「跑之前有几条、跑完还是几条」。用户的阅读偏好
     也先快照后还原（脚本要把 `er:prefs` 清空才能测默认宽度 380）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

BACKEND = "http://127.0.0.1:8010"
FRONTEND = os.environ.get("FRONT_URL", "http://127.0.0.1:5173")

POP = '[data-testid="selection-popover"]'
PANEL = '[data-testid="ai-panel"]'
RESIZER = '[data-testid="panel-resizer"]'

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


def api_delete_highlight(highlight_id: int) -> None:
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        c.delete(f"/api/highlights/{highlight_id}")


def pick_title_block(art: dict) -> dict | None:
    return next((b for b in art["blocks"] if b["type"] == "title"), None)


def pick_fixtures(title: str) -> tuple[str, str, str]:
    """从标题里挑三个夹具：短语、词、词内片段。

    必须从**标题本身**派生，不能写死 `Mogao Grottoes` —— 换一篇文章夹具就没了。
    三条约束：
      * 词长 ≥ 6 且在标题里唯一（脚本靠 `indexOf` 定位，撞车会选到别处）；
      * 短语 = 词 + 紧跟其后的一个词，**保证词嵌在短语里**（嵌套是高亮切分里
        最容易出错的一档，得让它必然发生）；
      * 片段 = 词去掉首字母，用来验「半截词吸附到整词」。
    """
    for m in re.finditer(r"[A-Za-z][A-Za-z'’-]*", title):
        word = m.group(0)
        if len(word) < 6 or title.count(word) != 1:
            continue
        nxt = re.match(r"\s+([A-Za-z][A-Za-z'’-]*)", title[m.end():])
        if not nxt:
            continue
        phrase = f"{word} {nxt.group(1)}"
        frag = word[1:]
        if title.count(phrase) == 1 and title.count(frag) == 1:
            return phrase, word, frag
    return "", "", ""


# ---------------------------------------------------------------- 页面工具

# 建选区 + 立刻派发 mouseup（同一个 evaluate，不让选区有机会失效）。
# 文本按「整块拼接」定位，块里已有 <mark> 把句子切成多个节点也能选。
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
  if (before.top < 140) window.scrollBy(0, before.top - Math.round(window.innerHeight * 0.3));
  if (fireMouseup) el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  const after = r.getBoundingClientRect();
  return { ok: true, text: r.toString(), x: after.left, y: after.top };
}
"""

# 正文里所有高亮，连同它相对所属块的字符偏移与归属的痕迹 id
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

PANEL_W_JS = f"""() => {{
  const el = document.querySelector('{PANEL}');
  return el ? el.getBoundingClientRect().width : -1;
}}"""


async def wait_for(page, selector: str, timeout: int) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def expected_segments(
    title: str,
    phrase_span: tuple[int, int],
    word_span: tuple[int, int],
    phrase_id: int,
    word_id: int,
) -> list[tuple[int, str, int]]:
    """「刷新后标题该被切成哪几段」的期望值。

    规则与 `lib/selection.ts::splitByHighlights` 一致：按所有边界点切段，
    每段归给「覆盖它、且区间更短」的那一条（嵌套时内层赢），相邻同归属的段并回去。
    这里只覆盖本脚本用到的情形（两条真实痕迹、没有负数定位标记）——
    不打算复刻整个算法，否则测试会和实现一起漂。
    """
    cuts = sorted({phrase_span[0], phrase_span[1], word_span[0], word_span[1]})
    out: list[tuple[int, str, int]] = []
    for a, b in zip(cuts, cuts[1:]):
        if b <= a:
            continue
        cover = [
            (hid, span)
            for hid, span in ((word_id, word_span), (phrase_id, phrase_span))
            if span[0] <= a and span[1] >= b
        ]
        hid = min(cover, key=lambda h: h[1][1] - h[1][0])[0]
        if out and out[-1][2] == hid:
            out[-1] = (out[-1][0], out[-1][1] + title[a:b], hid)
        else:
            out.append((a, title[a:b], hid))
    return sorted(out)


def search_title_hit(aid: int, tid: int, title: str) -> dict | None:
    """找一个「检索命中标题块」的深链目标。

    标题块进了 `fts_blocks`（`save_fetch` 与补行迁移都写了），所以它现在是可检索的；
    点检索结果应当能跳回标题并闪一下。词从标题里现挑：长一点的、在标题里唯一的优先。
    """
    cands = sorted(
        {w for w in re.findall(r"[A-Za-z][A-Za-z'’-]{3,}", title)},
        key=len,
        reverse=True,
    )
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        for w in cands[:6]:
            hits = c.get("/api/search", params={"q": w}).json().get("blocks") or []
            hit = next((h for h in hits if h["block_id"] == tid and h["start"] is not None), None)
            if hit:
                return {"word": w, **hit}
    return None


async def open_panel(page) -> bool:
    """打开右栏（面板状态不持久化，每次刷新后都要重新开）。"""
    try:
        await page.click("text=打开 AI 面板", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    return await wait_for(page, PANEL, 8000)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    art = pick_article(args.article_id)
    aid = art["id"]
    title_block = pick_title_block(art)
    if title_block is None:
        print("这篇没有标题块 —— 老库要靠 db.init_db() 的标题块迁移补上")
        sys.exit(2)
    tid = title_block["id"]
    title = title_block["text"] or ""
    phrase, word, frag = pick_fixtures(title)
    if not phrase:
        print(f"标题里挑不出夹具（需要 ≥6 字母、唯一的词）：{title!r}")
        sys.exit(2)
    before = {h["id"] for h in api_highlights(aid)}
    print(f"  样本：#{aid} 标题块={tid} 标题={title[:52]!r}")
    print(f"  夹具：短语={phrase!r} 词={word!r} 片段={frag!r}")
    print(f"  跑之前的痕迹：{len(before)} 条", flush=True)

    created: list[int] = []
    js_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        await page.goto(f"{FRONTEND}/a/{aid}", wait_until="networkidle")
        # 宽度那一节要验默认值 380，先清掉本机偏好；用户的偏好跑完还原（文件末尾）
        prefs_backup = await page.evaluate("() => localStorage.getItem('er:prefs')")
        await page.evaluate("() => localStorage.removeItem('er:prefs')")
        await page.reload(wait_until="networkidle")
        await page.wait_for_selector(f'h1[data-block-id="{tid}"]', timeout=25000)

        # ---------------------------------------------------------- A 标题是块
        head("[A] 标题就是正文里的一个块")
        info = await page.evaluate(
            """(tid) => {
              const el = document.querySelector(`h1[data-block-id="${tid}"]`);
              return {
                text: el.textContent,
                tag: el.tagName,
                total: document.querySelectorAll(`[data-block-id="${tid}"]`).length,
                inProse: document.querySelectorAll(`.prose-en [data-block-id="${tid}"]`).length,
              };
            }""",
            tid,
        )
        check(info["text"] == title, "h1 上的文本与块文本逐字一致", repr(info["text"])[:52])
        check(info["tag"] == "H1", "标签仍是 h1（没降级成正文小标题）", info["tag"])
        check(info["total"] == 1 and info["inProse"] == 0,
              "标题只渲染一次（BlockRenderer 跳过了它）",
              f"共 {info['total']} · 正文里 {info['inProse']}")

        # ---------------------------------------------------------- B 标题划句
        head("[B] 在标题里划句 → 浮层，但不落库（没点【解析】就当没划过）")
        r = await page.evaluate(SELECT_JS, [tid, phrase, True])
        check(bool(r.get("ok")) and r.get("text") == phrase, "在标题里建立了选区",
              json.dumps(r)[:78])
        check(await wait_for(page, POP, 8000), "浮层出现")
        pop = await page.inner_text(POP) if await page.locator(POP).count() else ""
        check(phrase in pop, "浮层里是标题的这段文字", pop.replace("\n", " / ")[:66])
        check(await page.locator('[data-testid="parse-action"]').count() > 0,
              "浮层给出【解析】入口（选段不自动调模型）")
        check(await page.locator(f'{POP} >> text=取消标记').count() == 0,
              "没点过【解析】的选段没有「取消标记」可点")

        pstart = title.index(phrase)
        marks = [m for m in await page.evaluate(READ_MARKS_JS) if m["blockId"] == tid]
        check(len(marks) == 0, "划中不落痕：标题里还没有高亮",
              json.dumps(marks, ensure_ascii=False)[:84])
        check(not [h for h in api_highlights(aid)
                   if h["block_id"] == tid and h["id"] not in before],
              "划中不落库：本次没有产生标题痕迹")
        await page.keyboard.press("Escape")

        # C/D 要验「嵌套切分」，得先有一条真实的短语痕迹 —— 等价于用户点了
        # 一次【解析】（这里直接走接口，不真调模型）。接口造的痕迹前端看不到，
        # 刷新一次让高亮按区间重建。
        with httpx.Client(base_url=BACKEND, timeout=30) as c:
            resp = c.post(
                f"/api/articles/{aid}/highlights",
                json={"block_id": tid, "start_offset": pstart,
                      "end_offset": pstart + len(phrase),
                      "kind": "phrase", "surface": phrase},
            )
        check(resp.status_code == 201, "落一条短语痕迹（等价于点过【解析】）",
              f"HTTP {resp.status_code}")
        if resp.status_code == 201:
            h = resp.json()
            created.append(int(h["id"]))
            check((h["start_offset"], h["end_offset"]) == (pstart, pstart + len(phrase)),
                  "库里的区间 = 标题里的真实区间", f"{h['start_offset']}~{h['end_offset']}")
            check(h["kind"] == "phrase" and h["surface"] == phrase, "归类与原文都对", h["kind"])
            check(h["block_type"] == "title", "痕迹认得出自己挂在标题上", str(h["block_type"]))

        await page.reload(wait_until="networkidle")
        await page.wait_for_selector(f'h1[data-block-id="{tid}"]', timeout=20000)
        marks = [m for m in await page.evaluate(READ_MARKS_JS) if m["blockId"] == tid]
        check(len(marks) == 1 and marks[0]["text"] == phrase, "标题里画出了高亮",
              json.dumps(marks, ensure_ascii=False)[:84])
        check(bool(marks) and marks[0]["offset"] == pstart, "高亮的块内偏移 = 标题里的真实位置",
              f"{marks[0]['offset'] if marks else '—'} vs {pstart}")

        # ---------------------------------------------------------- C 标题划词
        head("[C] 在标题里划词（半截词吸附整词；词典/模型请求已掐断）")
        # 掐断本地词典请求：这一节只验「标题能不能划、能不能落库」，
        # 不需要真查词典（M10 之前这里还掐了 /api/ai/dict，那个端点已下线）
        await page.route("**/api/dict/**", lambda route: asyncio.ensure_future(route.abort()))
        r2 = await page.evaluate(SELECT_JS, [tid, frag, True])
        check(bool(r2.get("ok")), f"在标题里选了词内片段 {frag!r}", json.dumps(r2)[:56])
        check(await wait_for(page, POP, 8000), "浮层出现")
        shown = await page.evaluate(
            """() => {
              const p = document.querySelector('[data-testid="selection-popover"]');
              const s = p.querySelector('.text-base');
              return {
                surface: s ? s.textContent : '',
                snapped: !!p.querySelector('[data-testid="snapped-note"]'),
                kind: p.dataset.kind,
              };
            }"""
        )
        check(shown["surface"] == word, "片段被吸附到整词", repr(shown["surface"]))
        check(shown["snapped"] and shown["kind"] == "word",
              "浮层标明「已按整词查」且按词处理",
              f"kind={shown['kind']} snapped={shown['snapped']}")

        wstart = title.index(word)
        wrows = [h for h in api_highlights(aid) if h["block_id"] == tid and h["surface"] == word]
        check(len(wrows) == 1, "整词的痕迹落库")
        if wrows:
            created.append(wrows[0]["id"])
            check((wrows[0]["start_offset"], wrows[0]["end_offset"]) == (wstart, wstart + len(word)),
                  "整词区间正确", f"{wrows[0]['start_offset']}~{wrows[0]['end_offset']}")

        marks2 = [m for m in await page.evaluate(READ_MARKS_JS) if m["blockId"] == tid]
        kinds = {m["kind"]: m["bg"] for m in marks2}
        check(len(marks2) == 2, "标题里两条高亮同时存在", str(len(marks2)))
        check(len(set(kinds.values())) == 2, "词与句是两档底色（嵌套时内层赢）",
              json.dumps(kinds, ensure_ascii=False))

        # ---------------------------------------------------------- D 刷新重建
        head("[D] 刷新：标题的高亮按区间重建，偏移不变")
        await page.reload(wait_until="networkidle")
        await page.wait_for_selector(f'h1[data-block-id="{tid}"]', timeout=20000)
        m3 = [m for m in await page.evaluate(READ_MARKS_JS) if m["blockId"] == tid]
        got = sorted((m["offset"], m["text"], m["hlId"]) for m in m3)
        # 嵌套时外层短语会被内层的词切开（内层赢），所以按「段」对账而不是按整条 ——
        # 一条高亮渲染成几个 <mark> 是重建的正常结果，区间对得上才是重点。
        expect = expected_segments(
            title,
            (pstart, pstart + len(phrase)),
            (wstart, wstart + len(word)),
            created[0],
            created[1],
        )
        check(got == expect, "两条痕迹的段落与刷新前逐字一致",
              json.dumps(got, ensure_ascii=False))
        check(len(js_errors) == 0, "控制台无 JS 报错", "; ".join(js_errors[:2]))

        # ---------------------------------------------------------- E 右栏拖动
        head("[E] 右栏：鼠标移到边框最左侧能拖宽")
        check(await open_panel(page), "右栏打开")
        w0 = await page.evaluate(PANEL_W_JS)
        check(abs(w0 - 380) < 1.5, "默认宽度 380", f"{w0:.1f}")
        cursor = await page.evaluate(
            f"() => getComputedStyle(document.querySelector('{RESIZER}')).cursor"
        )
        check(cursor == "col-resize", "边框上的光标是 col-resize", cursor)

        box = await page.locator(RESIZER).bounding_box()
        border_x = await page.evaluate(
            f"() => document.querySelector('{PANEL}').getBoundingClientRect().left"
        )
        check(box["width"] >= 8 and box["x"] <= border_x <= box["x"] + box["width"],
              "命中区跨在面板边框上（不是只有 1px）",
              f"x={box['x']:.1f}~{box['x'] + box['width']:.1f} 边框={border_x:.1f}")

        line_js = (
            f"() => getComputedStyle(document.querySelector('{RESIZER}'), '::before')"
            f".backgroundColor"
        )
        cx, cy = box["x"] + box["width"] / 2, box["y"] + 260
        await page.mouse.move(20, 400)
        await page.wait_for_timeout(250)          # 过渡 120ms，读数前先等它跑完
        idle_line = await page.evaluate(line_js)
        await page.mouse.move(cx, cy)
        await page.wait_for_timeout(250)
        hover_line = await page.evaluate(line_js)
        check(idle_line != hover_line and "rgba(0, 0, 0, 0)" in idle_line,
              "悬停时那条边框被点亮（提示这里能拉）", f"{idle_line} → {hover_line}")

        await page.mouse.down()
        await page.mouse.move(cx - 140, cy, steps=12)
        await page.mouse.up()
        w1 = await page.evaluate(PANEL_W_JS)
        check(abs(w1 - (w0 + 140)) < 6, "往左拖 140px → 宽度 +140", f"{w0:.1f} → {w1:.1f}")
        saved = await page.evaluate("() => JSON.parse(localStorage.getItem('er:prefs')).panelWidth")
        check(abs(saved - w1) < 2, "松手后宽度落进偏好（拖动中不写）", str(saved))

        await page.mouse.move(cx - 140, cy)
        await page.mouse.down()
        await page.mouse.move(cx + 260, cy, steps=12)
        await page.mouse.up()
        w2 = await page.evaluate(PANEL_W_JS)
        check(abs(w2 - 300) < 12, "往右拖到底被夹在下限 300", f"{w2:.1f}")

        await page.reload(wait_until="networkidle")
        check(await open_panel(page), "刷新后重新打开右栏")
        w3 = await page.evaluate(PANEL_W_JS)
        check(abs(w3 - w2) < 2, "刷新后面板宽度照旧", f"{w3:.1f}")

        box2 = await page.locator(RESIZER).bounding_box()
        await page.mouse.dblclick(box2["x"] + box2["width"] / 2, box2["y"] + 260)
        w4 = await page.evaluate(PANEL_W_JS)
        check(abs(w4 - 380) < 2, "双击复位回 380", f"{w4:.1f}")

        # ---------------------------------------------------------- F 检索回到标题
        head("[F] 检索命中标题 → 深链跳回来闪一下（顺带打通的一条链）")
        hit = search_title_hit(aid, tid, title)
        check(hit is not None, "检索能命中标题块（它进了 fts_blocks）",
              json.dumps(hit or {}, ensure_ascii=False)[:88])
        if hit:
            await page.goto(
                f"{FRONTEND}/a/{aid}?block={hit['block_id']}&start={hit['start']}&end={hit['end']}",
                wait_until="networkidle",
            )
            await page.wait_for_selector(f'h1[data-block-id="{tid}"]', timeout=25000)
            focus = await page.evaluate(
                """(tid) => {
                  const el = document.querySelector(`h1[data-block-id="${tid}"]`);
                  const m = el.querySelector('mark[data-focus="1"]');
                  return m ? { text: m.textContent, top: el.getBoundingClientRect().top } : null;
                }""",
                tid,
            )
            check(focus is not None and focus["text"] == hit["word"],
                  "标题里闪的是命中的那个词", json.dumps(focus, ensure_ascii=False))
            check(await page.locator('[data-testid="focus-hint"]').count() > 0,
                  "「已定位到检索命中的位置」提示出现")
            check(bool(focus) and focus["top"] > -40 and focus["top"] < 400,
                  "页面停在标题处（不用用户自己往上翻）",
                  f"top={(focus or {}).get('top')}")

        # 还原用户的阅读偏好（脚本开头为了测默认宽度把它清空过）
        if prefs_backup is None:
            await page.evaluate("() => localStorage.removeItem('er:prefs')")
        else:
            await page.evaluate(
                "(v) => localStorage.setItem('er:prefs', v)", prefs_backup
            )

        await browser.close()

    # ---------------------------------------------------------------- 收尾
    for hid in created:
        api_delete_highlight(hid)
    after = {h["id"] for h in api_highlights(aid)}
    check(after == before, "本次造的痕迹已删净，原有痕迹一条未动",
          f"{len(before)} 条 → {sorted(after)}")

    total = passed + len(failed)
    print("\n" + "=" * 62, flush=True)
    print(f"M9 浏览器验收：{passed}/{total} 通过")
    if failed:
        print("失败项：")
        for f in failed:
            print(f"  - {f}")
    print("=" * 62, flush=True)
    sys.exit(1 if failed else 0)


asyncio.run(main())
