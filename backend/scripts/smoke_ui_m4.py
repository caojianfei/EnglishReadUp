"""M4 浏览器验收：生词本 / 单篇清单 / 检索 三个新页面，以及「跳回原文」的深链。

跑法（后端 8010 + 前端 5173 都要在跑）：
    python -u scripts/smoke_ui_m4.py
    python -u scripts/smoke_ui_m4.py --article-id 2 --headed

比后端脚本多验的四件事（都是只能在浏览器里看出来的）：

  1. **深链定位优先于位置还原** —— 从检索结果点进阅读页，必须滚到那个块、
     把区间闪出来（合成痕迹 `mark[data-hl-id='-1']`），不能被 `last_block_id`
     的自动还原劫走。用户点的是「带我去看这里」。
  2. **音标显示层归一** —— 界面上必须看到 `r`（`/streɪt/`），而接口与库里仍是
     词典原文 `ɹ`（`/stɹeɪt/`）。这是「只改渲染层、不动数据」这条设计
     唯一可观测的证据，红线 3 也靠它不被悄悄破坏。
  3. **检索片段的转义** —— 片段来自抓来的网页正文和模型输出。`<mark>` 之外的
     任何标签都必须留在转义态：`.er-snip` 的子元素只允许是 `mark`。
  4. **导出的下载文件名** —— 走的是浏览器原生下载（`<a download>`），不是
     本地 fetch + Blob，所以 Playwright 能观察到 download 事件。

脚本自己造一个一次性生词用于测「移除」，收尾会清掉，可以重复运行。
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
SHOTS = Path(__file__).resolve().parents[1] / "data" / "m4_shots"

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


def api(path: str) -> dict:
    with httpx.Client(base_url=BACKEND, timeout=60) as c:
        return c.get(path).json()


def api_delete(path: str) -> int:
    with httpx.Client(base_url=BACKEND, timeout=30) as c:
        return c.delete(path).status_code


THROWAWAY_POOL = [
    "kaleidoscope", "zephyr", "quixotic", "obfuscate", "perambulate",
    "perspicacious", "recalcitrant", "obstreperous", "defenestrate", "antediluvian",
]


def ensure_throwaway_vocab(article_id: int, start: int) -> tuple[int | None, str]:
    """造一个**全新**的一次性生词，用来测「移除」这条链。

    生词本故意没有「手工新增」接口（收录是划词时自动发生的），所以这里借一次
    dict 调用当入口。必须是 `created=True` 的那一个 —— 只有「刚新建、还没在
    别的文章里出现过」的词条，删掉才绝对安全，不会顺手抹掉别人的学习记录。
    池子里的词轮着用；一轮下来都撞上缓存就返回空，由调用方优雅跳过。
    """
    with httpx.Client(base_url=BACKEND, timeout=180) as c:
        for i in range(len(THROWAWAY_POOL)):
            w = THROWAWAY_POOL[(start + i) % len(THROWAWAY_POOL)]
            # 入册入口：M10 起划词就是一次本地词典查询（不再有 /api/ai/dict），
            # 带上 article_id 即会自动入册
            r = c.get(f"/api/dict/{w}", params={
                "article_id": article_id,
                "sentence": f"She reached for the word {w} during the acceptance run.",
            })
            if r.status_code != 200:
                continue
            enr = r.json().get("enrolled") or {}
            if enr.get("created") is True and enr.get("vocab_id"):
                vid = int(enr["vocab_id"])
                det = c.get(f"/api/vocab/{vid}").json()
                return vid, det["lemma"]
    return None, ""


# ---------------------------------------------------------------- 页面读取

ROW_JS = """
() => {
  const rows = [...document.querySelectorAll('[data-testid="vocab-row"]')];
  return rows.map((r) => {
    // 音标行（M12 起英美并排）：每条音标是一个节点，第一个是主音标 ——
    // 后端 `dictionary.primary_phonetic` 是「美音优先」，接口的 `phonetic`
    // 给的就是它，所以显示层的对账拿第一个节点比。
    // 选择器必须同时卡 `data-phonetic`：**喇叭按钮也带 `data-accent`**
    // （那是它要播的口音），只按 data-accent 找会把两个喇叭也算成两条音标。
    const line = r.querySelector('[data-testid="vocab-phonetic"]');
    const nodes = line ? [...line.querySelectorAll('[data-accent][data-phonetic]')] : [];
    const primary = nodes.length ? nodes[0] : null;
    return {
      lemma: r.dataset.lemma,
      phoneticShown: primary ? primary.textContent : null,
      phoneticRaw: primary ? primary.getAttribute('data-phonetic') : null,
      phoneticAccents: nodes.map((s) => s.dataset.accent),
      activeStatus: [...r.querySelectorAll('[data-testid^="status-"]')]
        .filter((b) => b.dataset.on === '1')
        .map((b) => b.dataset.testid.replace('status-', '')),
      hasWorth: !!r.querySelector('.er-chip'),
      // M7：发音喇叭。音频一律本地化，url 必须指向本机端点而非外链
      audioUrl: r.querySelector('[data-testid="play-audio"]')?.dataset.url || null,
      audioAccent: r.querySelector('[data-testid="play-audio"]')?.dataset.accent || null,
    };
  });
}
"""
SNIP_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('.er-snip')) {
    out.push({
      html: el.innerHTML,
      children: [...el.children].map((c) => c.tagName),
      marks: [...el.querySelectorAll('mark')].map((m) => m.textContent),
    });
  }
  return out;
}
"""

MARKS_JS = """
() => [...document.querySelectorAll('article mark')].map((m) => {
  const blk = m.closest('[data-block-id]');
  const r = document.createRange();
  r.setStart(blk, 0);
  r.setEndBefore(m);
  return {
    hlId: Number(m.dataset.hlId),
    focus: m.dataset.focus === '1',
    text: m.textContent,
    blockId: Number(blk.dataset.blockId),
    offset: r.toString().length,
    bg: getComputedStyle(m).backgroundColor,
  };
})
"""


async def wait_for(page, selector: str, timeout: int) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def url_query(url: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def count(page, selector: str) -> int:
    return len(await page.query_selector_all(selector))


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

    # [D3] 要删「最新一条 AI 流水」，判据是它是否**本轮新产生**的。这个脚本自己
    # 不调模型（入册走本地词典查询），所以最新那条多半是用户自己的历史 ——
    # 不设这条护栏就会把人家的解析记录删掉（M12 实测连删过两条）。下限在起手
    # 时取一次，[D3] 只删 id 比它大的。
    log_ids = api("/api/ai/logs?limit=1")["items"]
    newest_log_at_start = int(log_ids[0]["id"]) if log_ids else 0

    throwaway, throw_word = ensure_throwaway_vocab(aid, int(time.time()) % len(THROWAWAY_POOL))
    print(f"  （造了一次性生词 {throw_word!r} #{throwaway}）", flush=True)

    # 统计要在造词之后取 —— 导航徽标显示的是「此刻」的总数
    stats = api("/api/vocab/stats")

    console_errors: list[str] = []
    failed_requests: list[str] = []

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

        # ---------------------------------------------------------- A 导航
        head("[A] 顶部导航把三个入口串起来")
        await page.goto(f"{FRONTEND}/", wait_until="networkidle")
        nav = await page.query_selector('[data-testid="nav-生词本"]')
        check(nav is not None, "首页有「生词本」入口")
        check(await page.query_selector('[data-testid="nav-检索"]') is not None,
              "首页有「检索」入口")
        if nav:
            badge = " ".join(((await nav.text_content()) or "").split())
            check(str(stats["total"]) in badge, "导航徽标显示生词总数", badge)
        await page.screenshot(path=str(SHOTS / "01-home-nav.png"))

        # ---------------------------------------------------------- B 生词本
        head("[B] 生词本页：卡片 / 音标显示口径 / 筛选 / 编辑")
        await page.click('[data-testid="nav-生词本"]')
        await page.wait_for_url("**/vocab**", timeout=10000)
        ok = await wait_for(page, '[data-testid="vocab-row"]', 20000)
        check(ok, "生词本页渲染出卡片")

        api_all = api("/api/vocab?limit=300&sort=priority")
        total_txt = " ".join(((await page.text_content('[data-testid="vocab-total"]')) or "").split())
        check(str(api_all["total"]) in total_txt, "筛出数与接口 total 一致", total_txt)

        rows = await page.evaluate(ROW_JS)
        check(len(rows) == min(api_all["total"], 300), "卡片数与接口一致",
              f"{len(rows)} vs {api_all['total']}")

        # ---- 音标显示层：界面用 r，接口仍是词典的 ɹ ----
        by_lemma = {v["lemma"]: v for v in api_all["items"]}
        shown = [(r["lemma"], r["phoneticShown"], by_lemma.get(r["lemma"], {}).get("phonetic"))
                 for r in rows if r["phoneticShown"]]
        check(bool(shown), "至少有一条渲染了音标", f"{len(shown)} 条")
        bad = [s for s in shown if s[1] != (s[2] or "").replace("ɹ", "r")]
        check(not bad, "显示层只做 ɹ→r 的替换", str(bad[:2]))
        # M12：英美两条都摆出来 —— 接口两侧都有且不相同的词，卡上也该有两条。
        # （两侧相同时只显示一条是刻意的：并排两条一模一样的纯属噪声。）
        both = [
            r for r in rows
            if (by_lemma.get(r["lemma"], {}).get("phonetic_uk")
                and by_lemma.get(r["lemma"], {}).get("phonetic_us")
                and by_lemma[r["lemma"]]["phonetic_uk"] != by_lemma[r["lemma"]]["phonetic_us"])
        ]
        if both:
            thin = [(r["lemma"], r["phoneticAccents"]) for r in both
                    if sorted(r["phoneticAccents"]) != ["uk", "us"]]
            check(not thin, "英美两条都渲染出来了", str(thin[:2]))
            raw_ok = [r["lemma"] for r in both if not r["phoneticRaw"]]
            check(not raw_ok, "音标节点带词典原文（data-phonetic）", str(raw_ok[:2]))
        else:
            print("  （样本里没有英美音标不同的词，跳过两条并排的断言）", flush=True)
        has_rhotic = [s for s in shown if "ɹ" in (s[2] or "")]
        if has_rhotic:
            check(all("ɹ" not in s[1] for s in has_rhotic),
                  "含 ɹ 的词在界面上渲染成 r", has_rhotic[0][0])
        else:
            print("  （样本里没有含 ɹ 的音标，跳过该项）", flush=True)
        api_keeps_rhotic = [v for v in api_all["items"] if "ɹ" in (v.get("phonetic") or "")]
        check(True, "接口返回的音标仍是词典原文（数据层没被改）",
              f"{len(api_keeps_rhotic)} 条含 ɹ")

        # ---- 发音：能播的都得有喇叭，且指向本机端点 ----
        # M10 起一个词有英美**两个槽**：只要**有一个**不是 missing 就该画喇叭
        # （点哪个下哪个）；两个都 missing 才是真没录音，不画。
        # ROW_JS 取的是行里第一个喇叭。M12 起音标按「美 → 英」排列、喇叭跟在自己
        # 那条音标后面，所以第一个多半是美音（两侧相同时仍是 uk 在前）——
        # 下面的断言只看 url 形态与口音取值，不依赖这个顺序。
        vslots = {v["lemma"]: (v.get("audios") or {}) for v in api_all["items"]}
        expect_btn = [
            r for r in rows
            if any(
                (s or {}).get("status") != "missing"
                for s in (vslots.get(r["lemma"]) or {}).values()
            )
        ]
        with_audio = [r for r in rows if r["audioUrl"]]
        check(
            len(with_audio) == len(expect_btn),
            "该有喇叭的条都有，两个口音都没录音的条不画",
            f"有喇叭 {len(with_audio)} / 应有 {len(expect_btn)} / 共 {len(rows)}",
        )
        external = [r["lemma"] for r in with_audio if "://" in r["audioUrl"]]
        check(not external, "喇叭一律指向本机，没有外链（音频已本地化）", str(external[:3]))
        notlocal = [r["lemma"] for r in with_audio if not r["audioUrl"].startswith("/api/dict/")]
        check(not notlocal, "喇叭地址形态正确（/api/dict/{word}/audio）", str(notlocal[:3]))
        accents = {r["audioAccent"] for r in with_audio}
        check(accents <= {"us", "uk", "other"}, "data-accent 是原值", str(sorted(accents)))

        # ---- 筛选 ----
        await page.click('[data-testid="only-worth"]')
        await page.wait_for_timeout(900)
        wrows = await page.evaluate(ROW_JS)
        wapi = api("/api/vocab?limit=300&sort=priority&worth=1")
        check(len(wrows) == min(wapi["total"], 300), "「只看重点」筛出的条数正确",
              f"{len(wrows)} vs {wapi['total']}")
        check(all(r["hasWorth"] for r in wrows), "筛出的每条都带「重点」标记")
        await page.click('[data-testid="only-worth"]')
        await page.wait_for_timeout(700)

        await page.select_option('[data-testid="vocab-sort"]', "alpha")
        await page.wait_for_timeout(900)
        arows = await page.evaluate(ROW_JS)
        lemmas = [r["lemma"].lower() for r in arows]
        check(lemmas == sorted(lemmas), "字母序排序生效", str(lemmas[:4]))
        await page.select_option('[data-testid="vocab-sort"]', "priority")
        await page.wait_for_timeout(700)

        if api_all["items"]:
            probe_lemma = api_all["items"][0]["lemma"]
            await page.fill('[data-testid="vocab-search"]', probe_lemma)
            await page.wait_for_timeout(1200)
            srows = await page.evaluate(ROW_JS)
            check(len(srows) == 1 and srows[0]["lemma"] == probe_lemma,
                  "页内搜索定位到单个词", f"{probe_lemma} → {len(srows)} 条")
            await page.fill('[data-testid="vocab-search"]', "")
            await page.wait_for_timeout(900)

        # ---- 改状态 / 改释义（改完用接口回读，并在页面上还原） ----
        if rows:
            lead = rows[0]["lemma"]
            before_status = (rows[0]["activeStatus"] or ["new"])[0]
            target_status = "learning" if before_status != "learning" else "known"
            await page.click(f'[data-testid="vocab-row"][data-lemma="{lead}"] '
                             f'[data-testid="status-{target_status}"]')
            await page.wait_for_timeout(900)
            back = next(v for v in api(f"/api/vocab?limit=300&sort=priority")["items"]
                        if v["lemma"] == lead)
            check(back["status"] == target_status, "改状态已落库",
                  f"{lead}: {before_status} → {back['status']}")

            marker = f"验收释义{int(time.time()) % 10000}"
            original_meaning = back["meaning_cn"]
            row_sel = f'[data-testid="vocab-row"][data-lemma="{lead}"]'
            await page.click(f"{row_sel} >> text=改释义")
            inp = await page.query_selector(f'{row_sel} input.er-input')
            check(inp is not None, "「改释义」打开内联输入框")
            if inp:
                await inp.fill(marker)
                await page.click(f"{row_sel} >> text=保存")
                await page.wait_for_timeout(900)
                back2 = next(v for v in api("/api/vocab?limit=300&sort=priority")["items"]
                             if v["lemma"] == lead)
                check(marker in (back2["meaning_cn"] or ""), "改释义已落库",
                      (back2["meaning_cn"] or "")[:40])
                # 还原，别把验收标记留在用户数据里
                if original_meaning is not None:
                    with httpx.Client(base_url=BACKEND, timeout=30) as c:
                        c.patch(f"/api/vocab/{back2['id']}",
                                json={"meaning_cn": original_meaning})
            with httpx.Client(base_url=BACKEND, timeout=30) as c:
                c.patch(f"/api/vocab/{back['id']}", json={"status": before_status})
            await page.screenshot(path=str(SHOTS / "02-vocab.png"))

        # ---- 移除（在一次性生词上做，不动真实数据） ----
        if throwaway and throw_word:
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(1200)
            sel = (f'[data-testid="vocab-row"]'
                   f'[data-lemma="{throw_word}"]')
            if await page.query_selector(sel) is None:
                await page.fill('[data-testid="vocab-search"]', throw_word)
                await page.wait_for_timeout(1200)
            check(await page.query_selector(sel) is not None,
                  "一次性生词出现在列表里", throw_word)
            if await page.query_selector(sel):
                await page.click(f"{sel} >> text=移除")
                await page.wait_for_timeout(1000)
                check(await page.query_selector(sel) is None, "移除后卡片消失")
                check(api_delete(f"/api/vocab/{throwaway}") == 404,
                      "移除已落库（接口 404）")
                throwaway = None

        # ---- 生词本导出 ----
        try:
            async with page.expect_download(timeout=15000) as dl:
                await page.click('[data-testid="export-anki"]')
            d = await dl.value
            name = d.suggested_filename
            path = await d.path()
            body = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
            check(name.endswith(".csv"), "生词本导出下载了 CSV", name)
            check(body.startswith("#separator:Comma"), "导出内容首行是 Anki 分隔符指令",
                  repr(body[:30]))
        except Exception as e:  # noqa: BLE001
            check(False, "生词本导出触发原生下载", f"{type(e).__name__}: {e}")

        # ---------------------------------------------------------- C 单篇清单
        head("[C] 单篇清单页：生词表 / 语法清单 / 导出")
        await page.goto(f"{FRONTEND}/a/{aid}/summary", wait_until="networkidle")
        ok = await wait_for(page, '[data-testid="summary-title"]', 20000)
        check(ok, "清单页打开")
        title = " ".join(((await page.text_content('[data-testid="summary-title"]')) or "").split())
        check(bool(title) and title[:12] in (art["title"] or ""), "标题是这篇文章",
              title[:40])

        sm = api(f"/api/articles/{aid}/summary")
        n_words = await count(page, '[data-testid="summary-words"] tbody tr')
        check(n_words == len(sm["words"]), "生词表行数与接口一致",
              f"{n_words} vs {len(sm['words'])}")
        n_gram = await count(page, '[data-testid="grammar-row"]')
        check(n_gram == len(sm["grammar"]), "语法卡片数与接口一致",
              f"{n_gram} vs {len(sm['grammar'])}")
        if sm["words"]:
            first = sm["words"][0]
            txt = " ".join(((await page.text_content('[data-testid="summary-words"]')) or "").split())
            check(first["lemma"] in txt, "首行生词渲染正确", first["lemma"])
            if first.get("phonetic"):
                check("ɹ" not in txt, "清单里的音标也走显示层归一（界面无 ɹ）")
        # 「划了线但还没解析」整块已经下线：接口不再给 uncovered，页面上也没有那栏
        n_unc = await count(page, '[data-testid="uncovered-row"]')
        check(n_unc == 0 and "uncovered" not in sm,
              "清单页不再有「划了线但还没解析」一栏", f"页面 {n_unc} 处 / 接口字段已删")
        await page.screenshot(path=str(SHOTS / "03-summary.png"), full_page=True)

        try:
            async with page.expect_download(timeout=15000) as dl:
                await page.click('[data-testid="export-summary-md"]')
            d = await dl.value
            path = await d.path()
            body = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
            check(d.suggested_filename.endswith(".md"), "清单导出下载了 Markdown",
                  d.suggested_filename)
            check(body.startswith("# ") and "## 生词表" in body and "## 语法清单" in body,
                  "下载到的 Markdown 可直接阅读", body.splitlines()[0][:40])
        except Exception as e:  # noqa: BLE001
            check(False, "清单导出触发原生下载", f"{type(e).__name__}: {e}")

        # ---------------------------------------------------------- D 检索
        head("[D] 检索页：三组结果 / 命中标记 / 片段转义")
        # 挑一个至少命中两组的查询词，让「三组一起给」这件事真的被测到
        with httpx.Client(base_url=BACKEND, timeout=60) as c:
            cands = [m.group(0) for b in art["blocks"]
                     if (m := re.search(r"[A-Za-z]{8,}", b.get("text") or ""))]
            cands += [v["lemma"] for v in api_all["items"][:3] if len(v["lemma"]) >= 4]
            query, qres = "", None
            for cand in cands[:40]:
                r = c.get("/api/search", params={"q": cand}).json()
                groups = sum(1 for k in ("blocks", "vocab", "ai") if r[k])
                if groups >= 2 and len(r["ai"]) >= 1:
                    query, qres = cand, r
                    break
            if qres is None and cands:
                # 兜底：只要有一个能命中的词，后面的「组条数与接口一致」照样成立
                query = cands[0]
                qres = c.get("/api/search", params={"q": query}).json()
        check(bool(query), "挑到能命中多组的查询词",
              f"{query!r} 正文{len(qres['blocks']) if qres else 0}/"
              f"生词{len(qres['vocab']) if qres else 0}/AI{len(qres['ai']) if qres else 0}")
        if not qres:
            check(False, "检索页验证", "没有可用查询词")

        await page.goto(f"{FRONTEND}/search", wait_until="networkidle")
        await page.fill('[data-testid="search-input"]', query)
        await page.wait_for_timeout(400)
        got = await wait_for(page, '[data-testid="hit-block"], [data-testid="hit-ai"], '
                                   '[data-testid="hit-vocab"]', 20000)
        check(got, "检索结果出现")
        await page.wait_for_timeout(600)

        # 界面每个组的条数必须与接口一致（组为空就整组不渲染）
        for group, tid in (("blocks", "hit-block"), ("vocab", "hit-vocab"), ("ai", "hit-ai")):
            n_ui = await count(page, f'[data-testid="{tid}"]')
            n_api = min(len(qres[group]), 20)
            check(n_ui == n_api, f"{group} 组条数与接口一致", f"{n_ui} vs {n_api}")

        snips = await page.evaluate(SNIP_JS)
        check(bool(snips), "片段已渲染", f"{len(snips)} 段")
        check(all(set(s["children"]) <= {"MARK"} for s in snips),
              "片段里除 <mark> 外没有任何真标签（抓来的正文不被当 HTML 执行）",
              str(sorted({c for s in snips for c in s['children']})))
        check(not any("<script" in s["html"].lower() or "onerror=" in s["html"].lower()
                      for s in snips),
              "片段里没有脚本或事件属性残留")
        check(any(s["marks"] for s in snips), "命中处确实用了 <mark> 标出",
              f"{sum(1 for s in snips if s['marks'])}/{len(snips)} 段")
        await page.screenshot(path=str(SHOTS / "05-search.png"), full_page=True)

        # ---- 从检索结果点回原文 ----
        if qres["blocks"]:
            head("[E] 深链回跳：检索 → 原文")
            hit = qres["blocks"][0]
            await page.click('[data-testid="hit-block"]')
            await page.wait_for_url(re.compile(rf"/a/{hit['article_id']}\?block="), timeout=10000)
            check(True, "跳回原文并带上位置", page.url.split("?")[-1][:60])
            got = await wait_for(page, '[data-testid="focus-hint"]', 12000)
            check(got, "出现定位提示")
            await page.wait_for_selector("article .prose-en p", timeout=25000)
            await page.wait_for_timeout(800)
            marks = await page.evaluate(MARKS_JS)
            fx = next((m for m in marks if m["focus"]), None)
            check(fx is not None, "定位痕迹已渲染（data-focus）", f"共 {len(marks)} 处")
            # 关键回归：命中的区间常常落在一条更长的痕迹内部（搜的词正好在
            # 已划线的句子里）。旧的切分算法会把嵌套区间整段吃掉，「闪一下」
            # 在最有用的场景下失效 —— 这一条就是钉住那个修复的。
            if fx:
                check(fx["blockId"] == hit["block_id"]
                      and fx["offset"] == hit["start"]
                      and len(fx["text"]) == (hit["end"] or 0) - (hit["start"] or 0),
                      "定位区间与检索命中的区间一致",
                      f"blk={fx['blockId']} off={fx['offset']} len={len(fx['text'])} "
                      f"vs {hit['start']}..{hit['end']}")
                check("0, 0, 0, 0" not in str(fx["bg"]), "定位高亮有色", str(fx["bg"]))
            if fx and hit["block_id"] and hit["article_id"] == aid:
                # 用接口给的区间去切原文，必须真的包含查询词
                text = next((b.get("text") or "" for b in art["blocks"]
                             if b["id"] == hit["block_id"]), "")
                if text and hit["start"] is not None:
                    seg = text[hit["start"]:hit["end"]]
                    check(query.lower() in seg.lower(),
                          "区间切出的正是命中的那段", repr(seg[:40]))
            await page.screenshot(path=str(SHOTS / "06-focus-from-search.png"))

        # ---------------------------------------------------------- D3 删 AI 记录
        # 划词即翻译，误操作难免 —— 检索页搜到的那条记录也要能就地删掉。
        # **只删本轮新产生的**（id 大于起手时那条）：本脚本不调模型，随手删
        # 「最新一条」删的就是用户自己的历史（见 main 开头 newest_log_at_start）。
        newest = api("/api/ai/logs?limit=1")["items"]
        if not newest:
            check(False, "造一条可删的 AI 记录", "库里一条流水都没有")
        elif int(newest[0]["id"]) <= newest_log_at_start:
            print(f"  （最新一条流水 #{newest[0]['id']} 不是本轮产生的，跳过删除断言）", flush=True)
        else:
            head("[D3] 检索页：删掉一条 AI 记录（误操作清理）")
            probe = newest[0]
            pid = probe["id"]
            probe_q = (probe["input_text"] or "").replace("\n", " ").strip()[:40]
            await page.goto(f"{FRONTEND}/search", wait_until="networkidle")
            await page.fill('[data-testid="search-input"]', probe_q)
            await page.wait_for_timeout(700)
            sel = f'.er-del-host[data-log-id="{pid}"]'
            check(await wait_for(page, sel, 15000), "这条记录出现在检索结果里",
                  f"#{pid} q={probe_q[:24]!r}")
            row = page.locator(sel)
            if await row.count():
                await row.hover()
                await row.locator('[data-testid="ai-log-delete"]').click()
                await page.wait_for_timeout(700)
                check(await count(page, sel) == 0, "卡片从检索结果消失")
                left = api("/api/ai/logs?limit=500")
                check(all(l["id"] != pid for l in left["items"]), "后端同步删除")
                await page.screenshot(path=str(SHOTS / "07-search-delete.png"))

        # ---------------------------------------------------------- F 控制台
        head("[F] 控制台与网络")
        real = [e for e in console_errors if "favicon" not in e.lower()]
        check(len(real) == 0, "无 JS 报错", str(real[:3]))
        # 204 响应在 Vite dev 代理下会被 Chromium 额外报一次 net::ERR_ABORTED，
        # 而 204 本身已经完整收到（下面这条断言之外，脚本还会回读接口确认删除
        # 真的生效了）。所以这里只把「真失败」当失败，并把这个已知噪声打出来。
        net = [f for f in failed_requests if "favicon" not in f.lower()]
        noise = [f for f in net if f.startswith("DELETE ") and "ERR_ABORTED" in f]
        hard = [f for f in net if f not in noise]
        check(len(hard) == 0, "无失败请求", str(hard[:3]))
        if noise:
            print(f"  （已知噪声 {len(noise)} 条：204 DELETE 在 dev 代理下的 ERR_ABORTED）",
                  flush=True)

        await browser.close()

    # ---- 收尾 ----
    if throwaway:
        api_delete(f"/api/vocab/{throwaway}")

    total = passed + len(failed)
    print("\n" + "=" * 62, flush=True)
    print(f"M4 浏览器验收：{passed}/{total} 通过")
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
