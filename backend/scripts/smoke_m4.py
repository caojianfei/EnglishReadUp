"""M4 后端验收：生词本 + 语法清单 + 汇总导出 + 三组检索。

跑法（后端需已在 8010 运行）：
    python scripts/smoke_m4.py                  # 自动挑一篇有正文的文章
    python scripts/smoke_m4.py --article-id 2
    python scripts/smoke_m4.py --offline        # 跳过需要真实 LLM 的项

验收重点（对应 design 需求 5/6 与红线）：
  需求 6 —— 「按篇产出 生词 + 语法 汇总清单」：清单里的每一行都必须能在
            highlights / vocab_sources / grammar_points 里找到出处。
  红线 3 —— 生词本的音标必须与 `/api/dict/{lemma}` 逐字相等；
            划变形词时取自**原形**（不是变形词自己的读音）。
  红线 1 —— 移除生词 / 语法点不牵连原文痕迹与 AI 流水（两层痕迹分离）。
  可检索 —— 正文 / 生词 / AI 三组都能命中，且每条都带跳回原文的位置。
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8010"

passed = 0
failed: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> bool:
    global passed
    if cond:
        passed += 1
        print(f"  \u2713 {name}" + (f"  [{detail}]" if detail else ""))
    else:
        failed.append(name)
        print(f"  \u2717 {name}" + (f"  [{detail}]" if detail else ""))
    return cond


def head(t: str) -> None:
    print(f"\n{t}")


WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{6,}")


def _sentence_around(text: str, start: int, end: int) -> str:
    """取包含 [start, end) 的完整句子。

    脚本把这个句子写进 `vocab_sources.sentence`，会跟着生词一起导出到
    Markdown / Anki。所以不能像早先那样简单切 ±60 字 —— 那样切出来的例句
    会从半个词开始（`st treasured artists, is sitting…`），看着像数据坏了。
    """
    before, after = text[:start], text[end:]
    s = max(before.rfind("."), before.rfind("!"), before.rfind("?"))
    ends = [x for x in (after.find("."), after.find("!"), after.find("?")) if x >= 0]
    e = min(ends) if ends else len(after)
    return re.sub(r"\s+", " ", before[s + 1:] + text[start:end] + after[:e + 1]).strip()[:400]


def pick_word(art: dict, avoid: set[str]) -> tuple[str, str]:
    """从正文里挑一个还没入过册的长词，连同它所在的句子一起返回。

    长词更容易撞上「词典里有音标」的情形；带 -ing/-ed 的优先，
    因为那正是音标最容易出错的一类（要走原形兜底）。
    """
    inflected: list[tuple[str, str]] = []
    others: list[tuple[str, str]] = []
    for b in art["blocks"]:
        text = b.get("text") or ""
        if len(text) < 40:
            continue
        for m in WORD_RE.finditer(text):
            w = m.group(0)
            if w.lower() in avoid:
                continue
            sentence = _sentence_around(text, m.start(), m.end())
            if re.search(r"(ing|ed)$", w, re.I):
                inflected.append((w, sentence))
            else:
                others.append((w, sentence))
            if len(inflected) >= 8 and len(others) >= 8:
                break
        if len(inflected) >= 8 and len(others) >= 8:
            break
    pool = inflected or others
    return pool[0] if pool else ("", "")


def main() -> None:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--offline", action="store_true", help="跳过真实 LLM 调用")
    args = ap.parse_args()

    global passed
    c = httpx.Client(base_url=BASE, timeout=180.0)

    # ------------------------------------------------------------ A 前置
    head("[A] 前置检查")
    h = c.get("/api/health").json()
    check(h.get("status") == "ok", "后端健康", h["llm"]["prompt_version"])
    check(isinstance(h.get("vocab"), dict) and "total" in h["vocab"], "health 带生词本统计", str(h.get("vocab")))

    if args.article_id:
        art = c.get(f"/api/articles/{args.article_id}").json()
    else:
        items = [a for a in c.get("/api/articles").json()["items"] if a["status"] == "done"]
        if not items:
            print("数据库里没有可用文章，先抓一篇：POST /api/articles {url}")
            sys.exit(2)
        items.sort(key=lambda a: -a["block_count"])
        art = c.get(f"/api/articles/{items[0]['id']}").json()
    aid = art["id"]
    check(bool(art.get("blocks")), "样本文章有正文", f"#{aid} 块={len(art['blocks'])}")

    # 挑一个「从来没被查过的词」。要同时避开两类：
    #   * 生词本里已有的词（lemma 与 surface 都算）—— 否则测不出「新建条目」；
    #   * 任何一次 dict 流水出现过的词形 —— 否则 input_hash 会命中上一条流水，
    #     走成「重复点开」那条路径（enrolled=None），B 节整段就白测了。
    avoid = {v["lemma"].lower() for v in c.get("/api/vocab?limit=500").json()["items"]}
    avoid |= {v["surface"].lower() for v in c.get("/api/vocab?limit=500").json()["items"]}
    avoid |= {
        (l.get("input_text") or "").split("\n")[0].strip().lower()
        for l in c.get("/api/ai/logs?limit=500").json()["items"]
        if l.get("prompt_type") == "dict"
    }
    word, sentence = pick_word(art, avoid)
    check(bool(word), "挑到未入册的目标词", f"{word!r}")
    if not word:
        print("  （正文里已没有没查过的长词，跳过入册相关断言）")

    # 这个词在正文里的确切字符区间：下面要走「换一条划词、同一段内容」的
    # 复用路径，必须有一条真实的痕迹（带 block_id + 偏移）才能复现。
    wblk: dict | None = None
    woff = 0
    for b in art["blocks"]:
        at = (b.get("text") or "").find(word)
        if word and at >= 0:
            wblk, woff = b, at
            break

    # ------------------------------------------------------------ B 自动入册
    head("[B] 划词 → 自动入册（需求 5/6 的连接点）")
    vocab_before = c.get("/api/vocab?limit=1").json()["total"]
    dict_resp = None
    hl_probe: int | None = None
    if word:
        # 入册入口：M10 起划词是一次本地词典查询（不再有 /api/ai/dict），
        # 带上 article_id 就自动入册。
        # 这节**不再受 --offline 影响**：本地词典不出网、不烧 token，
        # 没有理由再跳过它（原来跳过是因为它要真调一次模型）。
        r = c.get(f"/api/dict/{word}", params={"article_id": aid, "sentence": sentence})
        check(r.status_code == 200, "查词成功（本地词典）", f"HTTP {r.status_code}")
        if r.status_code == 200:
            dict_resp = r.json()
            enr = dict_resp.get("enrolled") or {}
            check(enr.get("enrolled") is True, "结果回执里带入册信息", str(enr))
            check(bool(enr.get("lemma")), "入册用的是 lemma", str(enr.get("lemma")))
            if enr.get("enrolled") is not True:
                print("  （这次没走到新建入册那条路径，跳过 B 节的后续断言）")

        if dict_resp and (dict_resp.get("enrolled") or {}).get("enrolled") is True:
            lemma = enr.get("lemma")

            def seen_now() -> int:
                return next(
                    x for x in c.get("/api/vocab", params={"q": lemma, "limit": 20}).json()["items"]
                    if x["lemma"] == lemma
                )["seen_count"]

            def row_now() -> dict:
                return next(
                    x for x in c.get("/api/vocab", params={"q": lemma, "limit": 20}).json()["items"]
                    if x["lemma"] == lemma
                )

            found = c.get("/api/vocab", params={"q": lemma, "limit": 5}).json()
            check(found["total"] >= 1, "生词本里查得到这个词", f"{found['total']} 条")
            row = next((x for x in found["items"] if x["lemma"] == lemma), None)
            check(row is not None, "词条对齐 lemma", lemma or "")
            if row:
                check(row["seen_count"] >= 1, "出现次数已记录", f"seen={row['seen_count']}")
                check(row["status"] in ("new", "learning", "known"), "状态合法", row["status"])
                check(row["worth"] in (0, 1), "worth 是 0/1", str(row["worth"]))

            total_after = c.get("/api/vocab?limit=1").json()["total"]
            check(total_after >= vocab_before, "生词本总数没变少",
                  f"{vocab_before} → {total_after}")

            # ---- 路径 a：同一个词在同一篇里重复查 -------------------------
            # 上一次已经入过册，这次只更新台账、不再新建（否则用户点十次，
            # 生词本里就多十条「划过」）。
            base = seen_now()
            r2 = c.get(f"/api/dict/{word}", params={
                "article_id": aid, "sentence": sentence,
            }).json()
            check((r2.get("enrolled") or {}).get("created") is False, "重复查不重复创建")
            check((r2.get("enrolled") or {}).get("enrolled") is True,
                  "重复查照样给入册回执（回执照旧是「这个词在生词本里」）",
                  str(r2.get("enrolled")))
            check(seen_now() == base, "同篇重复查不涨 seen_count", f"seen={base}")

            # ---- 路径 b：换了划词、内容相同 -------------------------------
            # 换个语境会让 input_hash 变（hash 里带 sentence），那就真会去调模型 ——
            # 那不是复用路径。要复现 `source=cache` 的复用，得是「同一段内容挂在
            # 另一条划词上」，也就是同一句在文章里被划了两次。
            if wblk is None:
                print("  （正文里定位不到这个词的字符区间，跳过复用路径断言）")
            else:
                hres = c.post(f"/api/articles/{aid}/highlights", json={
                    "block_id": wblk["id"], "start_offset": woff,
                    "end_offset": woff + len(word), "kind": "word", "surface": word,
                })
                check(hres.status_code == 201, "为本词造一条真实痕迹",
                      f"HTTP {hres.status_code}")
                if hres.status_code == 201:
                    hl_probe = hres.json()["id"]
                    base = seen_now()
                    r3 = c.get(f"/api/dict/{word}", params={
                        "article_id": aid, "sentence": sentence,
                        "highlight_id": hl_probe,
                    }).json()
                    check((r3.get("enrolled") or {}).get("enrolled") is True,
                          "带痕迹再查一次照样给回执", str(r3.get("enrolled")))
                    # 同篇 + 同 surface ⇒ 不算「又遇见一次」，所以计数**不该**涨。
                    # 这正是 M10 收紧的口径：反复点开同一个词不该把「划过次数」刷上去
                    # （它喂的是 sort=seen 与列表角标）。
                    check(seen_now() == base,
                          "同篇同形重复查不涨计数", f"{base} → {seen_now()}")
                    same_rows = [
                        x for x in c.get("/api/vocab", params={"q": lemma, "limit": 20}).json()["items"]
                        if x["lemma"] == lemma
                    ]
                    check(len(same_rows) == 1, "同 lemma 只有一行（不新增重复条目）",
                          f"{len(same_rows)} 行")
                    det = c.get(f"/api/vocab/{row_now()['id']}").json()
                    srcs = det.get("sources") or []
                    check(len(srcs) == len({(s.get("article_id"), s.get("surface"))
                                            for s in srcs}),
                          "出现记录没有重复行（同篇同形态只留一条）", f"{len(srcs)} 条")
                    check(row_now().get("article_count") == len({s.get("article_id")
                                                                 for s in srcs}),
                          "article_count 与出现记录里的篇数一致（同篇划两次只算一篇）",
                          f"{row_now().get('article_count')} vs {len({s.get('article_id') for s in srcs})}")
                    check(any(s.get("highlight_id") == hl_probe for s in srcs),
                          "新痕迹已挂到出现记录上")

    # ------------------------------------------------------------ C 音标口径
    head("[C] 生词本音标的口径（红线 3）")
    if dict_resp:
        # M10 起 `/api/dict/{word}` 返回的就是完整词条（字段在顶层，
        # 不再包一层 `result`），音标来自本地 ipa-dict / ECDICT。
        #
        # 音标口径也变了：**词形优先、原形兜底**。ipa-dict 收屈折形式
        # （ran / runs / running 各自成条），划到 `visitors` 拿到的就是它自己的
        # 读音 —— 人在文章里读的正是这个形式。只有词形没收录时才回退到原形，
        # 那时才记 `phonetic_of`。所以比对锚点是 `phonetic_of or word`，
        # 不是旧口径里的 `lemma`。
        res = dict_resp
        lemma = res["lemma"]
        surface = word
        anchor = res.get("phonetic_of") or res["word"]
        if res.get("phonetic_of"):
            check(res["phonetic_of"] == lemma, "音标回退到原形时记下原形",
                  f"{surface} → of={res['phonetic_of']}")
        d = c.get(f"/api/dict/{anchor}").json()
        check(res["phonetic"] == d["phonetic"],
              f"接口音标与词典端点逐字相等（取自 {anchor}）",
              f"{res['phonetic']!r} vs {d['phonetic']!r}")
        row = next(
            x for x in c.get(f"/api/vocab?q={lemma}&limit=5").json()["items"] if x["lemma"] == lemma
        )
        check(row["phonetic"] == res["phonetic"], "生词本存的音标与查词结果一致",
              f"{row['phonetic']!r}")
        check(row["phonetic_source"] in ("ipa", "ecdict", "missing", None),
              "音标来源可追溯", str(row["phonetic_source"]))
        # 生词本列表要画两个喇叭（M10）
        check(set(row.get("audios") or {}) == {"uk", "us"},
              "生词本条目带英美两个发音槽", str(sorted(row.get("audios") or {})))

    # ------------------------------------------------------------ D 列表与筛选
    head("[D] 生词本列表与筛选")
    allv = c.get("/api/vocab?limit=500").json()
    check(allv["total"] == len(allv["items"]), "total 与 items 一致",
          f"{allv['total']} / {len(allv['items'])}")
    if allv["items"]:
        keys = set(allv["items"][0].keys())
        for need in ("id", "lemma", "phonetic", "status", "worth", "seen_count",
                     "article_count", "article_titles", "sentence"):
            check(need in keys, f"列表字段含 {need}")
        got_articles = c.get(f"/api/vocab?article_id={aid}&limit=500").json()
        check(got_articles["total"] <= allv["total"], "按文章筛是子集",
              f"{got_articles['total']} ≤ {allv['total']}")
        # 按文章筛靠的是 vocab_sources（一词一行，出现记录分表），不是
        # vocab.article_id（那只是「第一次入册时所在的那篇」）。所以正向
        # 包含关系不成立，只能反过来校验 —— 汇总清单是本篇出处的权威口径，
        # 它列出的每个词都必须能被 article_id 筛出来。
        src = c.get(f"/api/articles/{aid}/summary").json()["words"]
        src_ids = {w["id"] for w in src}
        got_ids = {i["id"] for i in got_articles["items"]}
        check(src_ids <= got_ids, "本篇汇总里的每个词都能被按文章筛出",
              f"{len(src_ids)} ⊆ {len(got_ids)}")
        check(all(i["article_count"] >= 1 for i in got_articles["items"]),
              "筛出来的条目都带出现记录（不是靠 vocab.article_id 蒙的）",
              f"{got_articles['total']} 条")

        st = c.get("/api/vocab?status=known&limit=500").json()
        check(all(i["status"] == "known" for i in st["items"]), "状态筛选准确",
              f"{st['total']} 条")
        w1 = c.get("/api/vocab?worth=1&limit=500").json()
        check(all(i["worth"] == 1 for i in w1["items"]), "只看重点筛选准确", f"{w1['total']} 条")
        pri = c.get("/api/vocab?sort=priority&limit=500").json()["items"]
        check(pri == sorted(pri, key=lambda x: (-x["worth"], -x["seen_count"], -x["id"]))
              or len(pri) < 2, "按重要度排序生效")
        alpha = c.get("/api/vocab?sort=alpha&limit=500").json()["items"]
        check([x["lemma"].lower() for x in alpha] == sorted(x["lemma"].lower() for x in alpha)
              or len(alpha) < 2, "字母序排序生效")
        stats = c.get("/api/vocab/stats").json()
        check(stats["total"] == allv["total"], "stats.total 与列表一致",
              f"{stats['total']}")
        check(sum(stats["by_status"].values()) == stats["total"], "分状态之和等于总数",
              str(stats["by_status"]))

    # ------------------------------------------------------------ E 编辑与 FTS
    head("[E] 编辑生词 + FTS 同步")
    target = c.get("/api/vocab?limit=1&sort=priority").json()["items"]
    probe_word = None
    if target:
        vid = target[0]["id"]
        marker = f"验收标记{int(time.time()) % 100000}"
        up = c.patch(f"/api/vocab/{vid}", json={
            "status": "learning", "meaning_cn": f"保存；保留（{marker}）",
        })
        check(up.status_code == 200, "PATCH 释义与状态成功", f"HTTP {up.status_code}")
        if up.status_code == 200:
            body = up.json()
            check(body["status"] == "learning", "状态已改", body["status"])
            check(marker in (body["meaning_cn"] or ""), "释义已改", (body["meaning_cn"] or "")[:30])
            check(bool(body["updated_at"]), "updated_at 已刷新")
            # 改完立刻搜，验证 fts_vocab 跟着动了
            hit = c.get(f"/api/search?q={marker}").json()
            check(any(v["id"] == vid for v in hit["vocab"]),
                  "改过的释义立刻可检索（FTS 已同步）", f"{len(hit['vocab'])} 条命中")
        bad = c.patch(f"/api/vocab/{vid}", json={"status": "nonsense"})
        check(bad.status_code == 422, "非法状态被拒", f"HTTP {bad.status_code}")
        miss = c.patch("/api/vocab/99999999", json={"status": "known"})
        check(miss.status_code == 404, "不存在的生词 404", f"HTTP {miss.status_code}")
        det = c.get(f"/api/vocab/{vid}").json()
        check(isinstance(det.get("sources"), list) and det["sources"],
              "单条详情带出现记录", f"{len(det.get('sources') or [])} 条")

    # ------------------------------------------------------------ F 语法清单
    head("[F] 选段【解析】→ 语法清单（需求 6 的另一半）")
    gram_before = c.get(f"/api/grammar?article_id={aid}").json()["total"]
    # 要选一段**还没解析过**的原文：同一段内容再问一次会命中「已有流水」那条
    # 路径（enrolled=None，不去重写入册），那测的就不是入册了。
    used_seg = {
        g.get("source_sentence")
        for g in c.get(f"/api/grammar?article_id={aid}&limit=1000").json()["items"]
    }
    used_seg |= {
        (l.get("input_text") or "")
        for l in c.get(f"/api/ai/logs?article_id={aid}&limit=300").json()["items"]
        if l.get("prompt_type") == "explain"
    }
    # 解析允许合法地返回 0 条语法要点（这段确实没有可复用的结构），
    # 所以准备多个候选段：换着问到一段出点为止。入册本身必须有断言，不靠运气。
    segs: list[str] = []
    if not args.offline:
        for b in art["blocks"]:
            t = (b.get("text") or "").strip()
            if 80 <= len(t) <= 600 and t not in used_seg and t not in segs:
                segs.append(t)
            if len(segs) >= 3:
                break
        if not segs:
            print("  （本篇候选段落都已解析过，跳过入册断言）")

    seg = ""
    if segs:
        picked: dict | None = None
        why = "候选段都没产出语法要点"
        for cand in segs:
            r1 = c.post("/api/ai/explain", json={"article_id": aid, "text": cand})
            if r1.status_code != 200:
                why = f"解析失败：HTTP {r1.status_code} {r1.text[:120]}"
                break
            body = r1.json()
            points = (body.get("enrolled") or {}).get("points", 0)
            print(f"    → 本段语法要点入册 {points} 条")
            if points:
                seg, picked = cand, body
                break
        check(picked is not None, "解析成功且语法要点自动入清单",
              why if picked is None else "")
        if picked is not None:
            after = c.get(f"/api/grammar?article_id={aid}").json()
            check(after["total"] >= gram_before, "清单条数没变少",
                  f"{gram_before} → {after['total']}")
            check(all(g["article_id"] == aid for g in after["items"]),
                  "清单按文章隔离")
            check(all((g["explanation_cn"] or "").strip() for g in after["items"]),
                  "每条都有讲解（NOT NULL 约束）")
            cats = {g["category"] for g in after["items"]}
            allowed = {"时态", "语态", "从句", "非谓语", "倒装", "虚拟语气", "介词", "搭配", "其他", None}
            check(cats <= allowed, "类别已归一到既定语域", str(cats))

            # 重复解析同一段不重复入册（幂等）
            g2 = c.post("/api/ai/explain", json={"article_id": aid, "text": seg}).json()
            again = c.get(f"/api/grammar?article_id={aid}").json()["total"]
            check(again == after["total"], "重复解析不产生重复语法点",
                  f"{after['total']} → {again}（cached={g2['cached']}）")
            dup = [g["pattern"] for g in c.get(f"/api/grammar?article_id={aid}").json()["items"]]
            check(len(dup) == len(set(dup)), "同篇无重复结构", f"{len(dup)} 条")

    if gram_before or not args.offline:
        gp = c.get(f"/api/grammar?article_id={aid}").json()["items"]
        if gp:
            pid = gp[0]["id"]
            pu = c.patch(f"/api/grammar/{pid}", json={"category": "搭配"})
            check(pu.status_code == 200 and pu.json()["category"] == "搭配",
                  "语法点可改类别", f"HTTP {pu.status_code}")
            empty = c.patch(f"/api/grammar/{pid}", json={"explanation_cn": "  "})
            check(empty.status_code == 422, "空讲解被拒", f"HTTP {empty.status_code}")

    # ------------------------------------------------------------ G 汇总清单
    head("[G] 单篇汇总清单")
    s = c.get(f"/api/articles/{aid}/summary")
    check(s.status_code == 200, "汇总接口可用", f"HTTP {s.status_code}")
    if s.status_code == 200:
        sm = s.json()
        st = sm["stats"]
        check(st["words"] == len(sm["words"]), "stats.words 与实际一致",
              f"{st['words']} / {len(sm['words'])}")
        check(st["grammar"] == len(sm["grammar"]), "stats.grammar 与实际一致",
              f"{st['grammar']} / {len(sm['grammar'])}")
        check(sm["article"]["id"] == aid, "汇总指向正确的文章")
        for w in sm["words"]:
            check(w["article_id"] == aid, f"生词 {w['lemma']} 归属本篇")
            break
        # grammar 行本身没有 article_id 字段（查询里已经按篇过滤过），
        # 所以「归属本篇」改成对同源性和隔离性的校验：与 /api/grammar
        # 的按篇结果逐条一致，且与别篇的结果没有交集。
        g_list = c.get(f"/api/grammar?article_id={aid}&limit=1000").json()["items"]
        check({g["id"] for g in sm["grammar"]} == {g["id"] for g in g_list},
              "汇总语法清单与语法接口同源（同一批点）",
              f"{len(sm['grammar'])} 条")
        check([g["id"] for g in sm["grammar"]] == sorted(g["id"] for g in sm["grammar"]),
              "汇总里按 id 升序（阅读顺序），与列表页的倒序不同")
        others = [a["id"] for a in c.get("/api/articles").json()["items"] if a["id"] != aid]
        if others:
            oid = others[0]
            o_items = c.get(f"/api/grammar?article_id={oid}&limit=1000").json()["items"]
            check(not ({g["id"] for g in o_items} & {g["id"] for g in sm["grammar"]}),
                  "语法清单按文章隔离（与别篇无交集）", f"对比 #{oid}")
        check(sum(sm["grammar_by_category"].values()) == len(sm["grammar"]),
              "分类计数之和等于语法点总数", str(sm["grammar_by_category"]))
        # 「划了线但还没解析」不再是一栏：划一段不落痕，点过【解析】才有记录。
        # 这里造一条**没有解析记录挂靠**的孤立痕迹，确认它进不了清单
        # （没有 uncovered 字段，words / grammar 也不受影响）。
        check("uncovered" not in sm and "uncovered" not in st,
              "汇总不再有「待解析」一栏", str(sorted(sm)))
        blk = next(b for b in art["blocks"] if (b.get("text") or "").strip())
        t = blk["text"]
        hnew = c.post(f"/api/articles/{aid}/highlights", json={
            "block_id": blk["id"], "start_offset": 0,
            "end_offset": min(12, len(t)), "kind": "phrase", "surface": t[:12],
        })
        check(hnew.status_code == 201, "造一条孤立痕迹（没有解析记录）",
              f"HTTP {hnew.status_code}")
        if hnew.status_code == 201:
            hid = hnew.json()["id"]
            sm2 = c.get(f"/api/articles/{aid}/summary").json()
            check(sm2["words"] == sm["words"] and sm2["grammar"] == sm["grammar"],
                  "孤立痕迹不进清单（没解析过就没有条目）",
                  f"生词 {len(sm2['words'])} 条 / 语法 {len(sm2['grammar'])} 条")
            c.delete(f"/api/highlights/{hid}")
        nf = c.get("/api/articles/999999/summary")
        check(nf.status_code == 404, "不存在的文章 404", f"HTTP {nf.status_code}")

    # ------------------------------------------------------------ H 导出
    head("[H] 导出（Markdown 可读 / Anki CSV 可导入）")
    md = c.get(f"/api/articles/{aid}/export?format=md")
    check(md.status_code == 200, "汇总 Markdown 可下载", f"HTTP {md.status_code}")
    check("attachment" in md.headers.get("content-disposition", ""), "带下载文件名",
          md.headers.get("content-disposition", ""))
    body = md.text
    check(body.startswith("# "), "Markdown 以标题开头", body.splitlines()[0][:50])
    check("## 生词表" in body and "## 语法清单" in body, "两节都在")
    check("| --- |" in body, "表格分隔行合法")
    for line in body.splitlines():
        if line.startswith("|") and "---" not in line:
            check(line.count("|") >= 4, "表格行列数一致", line[:60])
            break
    check("|" not in body.split("\n")[0], "标题行没有多余竖线")

    anki = c.get(f"/api/articles/{aid}/export?format=anki")
    check(anki.status_code == 200, "Anki CSV 可下载", f"HTTP {anki.status_code}")
    text = anki.text
    lines = text.splitlines()
    check(lines[0] == "#separator:Comma", "首行是 Anki 分隔符指令（无 BOM）",
          repr(lines[0][:30]))
    check(lines[1] == "#html:true", "声明 html:true（背面用 <br>）")
    check(lines[2] == "#columns:front,back,tags", "声明三列")
    data = list(csv.reader(io.StringIO("\n".join(lines[3:]))))
    check(bool(data), "有数据行", f"{len(data)} 行")
    check(all(len(r) == 3 for r in data), "每行恰好三列",
          str({len(r) for r in data}))
    check(all((r[0] or "").strip() for r in data), "front 非空")
    check(all("english" in r[2] for r in data), "tags 带 english 标签")
    check(any("<b>" in r[1] for r in data), "背面含加粗音标（有音标的词）")
    check(all(not r[1].endswith("<br>") for r in data), "背面不以空行结尾")
    check(all(not any(ch in r[0] for ch in "ˈˌəɪʊæ") for r in data),
          "音标不出现在正面（先回忆读音，翻面才看答案）")
    txt = c.get(f"/api/articles/{aid}/export?format=text")
    check(txt.status_code == 200 and txt.text.startswith("# "), "原文 Markdown 走同一入口",
          f"HTTP {txt.status_code}")
    vcsv = c.get("/api/vocab/export?format=anki")
    check(vcsv.status_code == 200 and vcsv.text.startswith("#separator:Comma"),
          "生词本整体导出可用", f"HTTP {vcsv.status_code}")
    vmd = c.get("/api/vocab/export?format=md")
    check(vmd.status_code == 200 and "# 生词本" in vmd.text, "生词本 Markdown 可用")
    badfmt = c.get(f"/api/articles/{aid}/export?format=pdf")
    check(badfmt.status_code == 422, "未知格式被拒", f"HTTP {badfmt.status_code}")

    # ------------------------------------------------------------ I 三组检索
    head("[I] 三组检索与跳回原文")
    lemma = (dict_resp or {}).get("result", {}).get("lemma")
    if word:
        # 用**完整词形**搜，不用前五个字符：三张 FTS 表都是 porter 分词，
        # 索引里存的是词干（sitting → sit），所以 `sitt*` 匹配不到，
        # 而 `sitting` / `sit` 都能匹配到。那是分词器的正常行为。
        r = c.get("/api/search", params={"q": word}).json()
        check(r["q"] == word, "回显查询词")
        check(r["total"] == len(r["blocks"]) + len(r["vocab"]) + len(r["ai"]),
              "total = 三组之和", f"{r['total']}")
        check(len(r["ai"]) >= 1, "AI 组能命中", f"{len(r['ai'])} 条")
        # 正文组：目标词本来就在正文里，必然命中
        check(len(r["blocks"]) >= 1, "正文组能命中（词就在这篇文章里）",
              f"{len(r['blocks'])} 条")
        for a in r["ai"][:5]:
            check(bool(a["prompt_type"]) and bool(a["created_at"]), "AI 命中带类型与时间")
            break
        # 命中片段必须真的带 <mark>（否则用户看不出命中在哪）
        marked = [x for x in r["ai"] if "<mark>" in (x["snippet"] or "")]
        check(bool(marked), "AI 片段带命中标记", f"{len(marked)}/{len(r['ai'])}")

        # 正文命中的 start/end 必须能真的切出这个词
        blocks = {b["id"]: (b.get("text") or "") for b in art["blocks"]}
        ok = skipped = 0
        for b in r["blocks"][:5]:
            t = blocks.get(b["block_id"])
            if t is None or b["start"] is None:
                skipped += 1
                continue
            if word.lower() in t[b["start"]:b["end"]].lower():
                ok += 1
        check(ok == len(r["blocks"][:5]) - skipped and ok >= 1,
              "正文命中的区间能定位到这个词", f"{ok} 条（跳过 {skipped}）")
        check(all(b["article_id"] and b["block_id"] for b in r["blocks"]),
              "正文命中带跳转所需的位置")

        # 生词组：按 lemma 搜才稳（词形可能是变形词，不该指望它也命中）
        if lemma:
            rv = c.get("/api/search", params={"q": lemma}).json()
            check(any(v["lemma"] == lemma for v in rv["vocab"]),
                  "生词组能命中刚入册的词", f"{lemma} → {len(rv['vocab'])} 条")
            vmarked = [x for x in rv["vocab"] if "<mark>" in (x["snippet"] or "")]
            check(bool(vmarked), "生词片段带命中标记",
                  f"{len(vmarked)}/{len(rv['vocab'])}")
            vhit = next((v for v in rv["vocab"] if v["lemma"] == lemma), None)
            if vhit:
                check(bool(vhit.get("article_titles")),
                      "生词命中带「在哪几篇划过」", str(vhit.get("article_titles"))[:40])
                check(vhit.get("phonetic") or vhit.get("status"), "生词命中带卡片所需字段")

    # 前缀匹配：拿文章正文里确实存在的长词做前缀
    pre_base = ""
    for b in art["blocks"]:
        m = re.search(r"[A-Za-z]{8,}", b.get("text") or "")
        if m:
            pre_base = m.group(0)
            break
    if pre_base:
        pre = c.get("/api/search", params={"q": pre_base[:6]}).json()
        check(pre["total"] >= 1, "前缀匹配生效（输入半个词也能搜到）",
              f"{pre_base[:6]}… → {pre['total']}")
    # 中文走 LIKE 兜底
    cn = c.get("/api/search", params={"q": "保存"}).json()
    check(cn["total"] >= 1, "中文检索有兜底（unicode61 不切汉字）", f"{cn['total']} 条")
    # 特殊字符 / FTS5 关键词不能把查询语法搞崩：裸写 `OR*` 会直接 500，
    # 现在每个片段都加了引号变成普通词，这几条正是钉住那个回归的。
    for weird in ('"quoted - and OR (x)', "OR", "NOT x", "a OR b", "*", "()[]{}"):
        w = c.get("/api/search", params={"q": weird})
        check(w.status_code == 200, f"特殊查询不报错：{weird!r}",
              f"HTTP {w.status_code}")
    check(c.get("/api/search/suggest", params={"q": (word or "a")[:3]}).status_code == 200,
          "前缀补全可用")
    check(c.get("/api/search", params={"q": ""}).status_code == 422,
          "空查询被拒（q 是必填）")
    if hl_probe:
        c.delete(f"/api/highlights/{hl_probe}")

    # ------------------------------------------------------------ J 隔离
    head("[J] 两层痕迹互不牵连（红线 1）")
    if target:
        vid = target[0]["id"]
        hl_before = c.get(f"/api/articles/{aid}/highlights").json()["total"]
        logs_before = c.get(f"/api/ai/logs?article_id={aid}&limit=1").json()["total"]
        d = c.delete(f"/api/vocab/{vid}")
        check(d.status_code == 204, "移除生词成功", f"HTTP {d.status_code}")
        check(c.get(f"/api/vocab/{vid}").status_code == 404, "移除后查不到")
        check(c.get(f"/api/articles/{aid}/highlights").json()["total"] == hl_before,
              "原文高亮不受影响", f"{hl_before}")
        check(c.get(f"/api/ai/logs?article_id={aid}&limit=1").json()["total"] == logs_before,
              "AI 流水不受影响", f"{logs_before}")
        gone = c.get(f"/api/search?q={target[0]['lemma']}").json()
        check(not any(v["id"] == vid for v in gone["vocab"]), "移除后 FTS 里也搜不到")
    gp = c.get(f"/api/grammar?article_id={aid}").json()["items"]
    if gp:
        pid = gp[-1]["id"]
        n0 = c.get(f"/api/grammar?article_id={aid}").json()["total"]
        check(c.delete(f"/api/grammar/{pid}").status_code == 204, "移除语法点成功")
        check(c.get(f"/api/grammar?article_id={aid}").json()["total"] == n0 - 1,
              "清单条数 -1")
        check(c.delete(f"/api/grammar/{pid}").status_code == 404, "重复删除 404")

    print(f"\n{'=' * 56}")
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    if failed:
        for f in failed:
            print(f"  - {f}")
    c.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
