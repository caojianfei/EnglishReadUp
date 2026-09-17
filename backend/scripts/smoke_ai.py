"""M3 后端验收：划词痕迹 + 词典 + 四类 AI 任务 + 缓存 + 流水。

跑法（后端需已在 8010 运行）：
    python scripts/smoke_ai.py                 # 自动挑一篇有正文的文章
    python scripts/smoke_ai.py --article-id 2
    python scripts/smoke_ai.py --offline       # 跳过需要真实 LLM 的项

验收重点（对应 design 红线）：
  红线 3 —— dict 结果的 phonetic 必须**逐字等于**词典端点的返回值，
            证明音标过的是词典而不是模型。
  红线 4 —— 每次调用都有 ai_logs；同输入第二次必须命中缓存且不烧 token。
  需求 5 —— 划词即落库；删痕迹不删 AI 历史。
"""

from __future__ import annotations

import argparse
import json
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


def _audio_attempts(word: str, accent: str = "uk") -> int:
    """本地音频台账里该词**某个口音**被抓过几次。

    用它来证明「本地已有就不再走网络」比掐响应时间可靠：
    没有新请求 = attempts 不变，这是确定性的。
    M10 起台账是「一词一行一口音」（复合主键），所以必须带 accent 才唯一。
    """
    import sqlite3

    db = Path(__file__).resolve().parents[1] / "data" / "english.db"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT attempts FROM audio_cache WHERE word = ? AND accent = ?",
            (word.lower(), accent),
        ).fetchone()
        return int(row[0]) if row else -1
    finally:
        conn.close()


def _fts_ai_rows(log_id: int) -> int:
    """该记录在 FTS 索引里还剩几行。

    fts_ai 与 ai_logs 之间没有触发器（索引是 `_save_log` 手写的），
    所以「删记录」必须连索引一起清 —— 只删主表会在 FTS 里留下搜不到
    也管不着的孤儿行。这个断言钉住的就是那对成对删除。
    """
    import sqlite3

    db = Path(__file__).resolve().parents[1] / "data" / "english.db"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM fts_ai WHERE ai_log_id = ?", (log_id,)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def pick_article(c: httpx.Client, want: int | None) -> dict:
    if want is not None:
        r = c.get(f"/api/articles/{want}")
        r.raise_for_status()
        return r.json()
    items = c.get("/api/articles").json()["items"]
    done = [a for a in items if a["status"] == "done" and a["block_count"] > 3]
    if not done:
        print("数据库里没有可用文章，先抓一篇：POST /api/articles {url}")
        sys.exit(2)
    # 段落越多越好，能测到更复杂的句子
    done.sort(key=lambda a: -a["block_count"])
    return c.get(f"/api/articles/{done[0]['id']}").json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--offline", action="store_true", help="跳过真实 LLM 调用")
    args = ap.parse_args()

    global passed
    c = httpx.Client(base_url=BASE, timeout=180.0)

    # ------------------------------------------------------------ A 健康
    head("[A] 健康与配置")
    h = c.get("/api/health").json()
    check(h["status"] == "ok", "后端存活")
    check(h["llm"]["api_key_set"] is True, "LLM key 已配置",
          f"model={h['llm']['model']} v={h['llm']['prompt_version']}")
    if not args.offline:
        t0 = time.perf_counter()
        ai_h = c.get("/api/ai/health").json()
        ms = int((time.perf_counter() - t0) * 1000)
        ok = ai_h.get("ping", {}).get("ok")
        check(bool(ok), "LLM 真实连通", f"{ms}ms {ai_h.get('ping', {})}")

    art = pick_article(c, args.article_id)
    aid = art["id"]
    blocks = [b for b in art["blocks"] if b["type"] in ("para", "heading", "quote")]
    print(f"  样本：#{aid} {art['title'][:44]!r} 块={len(art['blocks'])}")

    # 记下开跑前最新一条流水的 id。G 段据此只校验「本次新写的」记录。
    # 不能要求全表版本一致 —— 老记录必须原样留着（需求 5），
    # 升 PROMPT_VERSION 的意义正是「旧记录不命中、但不删」。
    _pre = c.get("/api/ai/logs", params={"article_id": aid, "limit": 200}).json()
    log_id_before = max([l["id"] for l in _pre["items"]] or [0])
    cur_version = h["llm"]["prompt_version"]

    # ------------------------------------------------------------ B 本地词典
    head("[B] 本地词典与发音（红线 3 的源头）")
    print("  - 词条契约 / 双音标 / worth / 变形词兜底这些已搬到 scripts/smoke_m10.py")
    print("    （它拿 ipa-dict 文件与 stardict.db **逐字**比对，比在这里比接口更强）。")
    print("    这里只留「发音确实本地化」这条端到端证据。")
    da = c.get("/api/dict/documentary").json()
    slots = da.get("audios") or {}
    check(set(slots) == {"uk", "us"}, "词条带英美两个发音槽", str(sorted(slots)))
    check(
        all((slots.get(a) or {}).get("url", "").startswith("/api/dict/") for a in ("uk", "us")),
        "两个槽都给本机地址而非外链（浏览器不直连有道/Commons）",
        str({a: (slots.get(a) or {}).get("url") for a in ("uk", "us")}),
    )

    # 真去把音频取下来，验证「本地化」确实发生
    ra = c.get(f"/api/dict/{da['word']}/audio", params={"accent": "uk"}, timeout=60)
    check(ra.status_code == 200, "音频端点可取", f"HTTP {ra.status_code}")
    check(
        (ra.headers.get("content-type") or "").startswith("audio/"),
        "回的是音频而不是 JSON/HTML",
        ra.headers.get("content-type"),
    )
    check(len(ra.content) > 1024, "音频不是空壳", f"{len(ra.content)} B")
    check(
        ra.headers.get("x-audio-origin") == "youdao",
        "首选源命中 (youdao)",
        f"origin={ra.headers.get('x-audio-origin')} accent={ra.headers.get('x-audio-accent')}",
    )
    # mp3 要么带 ID3 头，要么裸流以帧同步字开头
    body = ra.content
    is_mp3 = body[:3] == b"ID3" or (body[0] == 0xFF and (body[1] & 0xE0) == 0xE0)
    check(is_mp3, "落盘的是真 mp3（不是 HTML 错误页）", body[:12].hex())

    # 第二次要**不发外网请求**就有：attempts 不再增长就是证据
    before = _audio_attempts(da["word"])
    again = c.get(f"/api/dict/{da['word']}/audio", timeout=30)
    check(
        again.status_code == 200 and again.content == body,
        "重复播放命中同一份本地文件",
        f"{len(again.content)} B",
    )
    check(
        _audio_attempts(da["word"]) == before,
        "重复播放没有再去抓（attempts 未增长）",
        f"{before} → {_audio_attempts(da['word'])}",
    )

    # 纯函数口径：地名标签要能归到英美，表外的（澳大利亚）落通用槽。
    # 这个归类决定了**回退候选的排序**（美 → 分不出 → 英），所以还得管。
    from app.services.dictionary import _audio_slots as _slots  # noqa: E402
    cases = [
        ("{{audio|en|en-us-x.ogg|Audio (US)}}", "us"),
        ("{{audio|en|en-uk-x.ogg|Audio (UK)}}", "uk"),
        ("{{audio|en|LL-Q1860 (eng)-A-b.wav|a=Southern England}}", "uk"),
        ("{{audio|en|LL-Q1860 (eng)-A-b.wav|a=New Jersey}}", "us"),
        ("{{audio|en|en-au-x.ogg}}", "any"),
        ("{{audio|en|x.ogg|a=Australia}}", "any"),
    ]
    for tpl, want in cases:
        uk, us, other = _slots(tpl)
        got = "uk" if uk else "us" if us else "any" if other else "none"
        check(got == want, f"口音归类 {want}", f"{tpl[:46]} → {got}")

    # ------------------------------------------------------------ C 划词痕迹
    head("[C] 划词痕迹（需求 5：划词即落库）")
    target = next(
        (b for b in blocks if len((b["text"] or "")) > 120),
        blocks[0] if blocks else None,
    )
    if target is None:
        print("  没有文本块，跳过 C~G")
        return
    text = target["text"]
    # 挑一个真实的词：取正文里第一个长度 > 6 的单词
    import re as _re
    m = _re.search(r"[A-Za-z]{7,}", text)
    ws, we = (m.start(), m.end()) if m else (0, min(8, len(text)))
    word = text[ws:we]

    h1 = c.post(f"/api/articles/{aid}/highlights", json={
        "block_id": target["id"], "start_offset": ws, "end_offset": we,
        "surface": word,
    })
    check(h1.status_code == 201, f"创建划词 {word!r}", f"HTTP {h1.status_code}")
    hi = h1.json()
    check(hi["surface"] == word and hi["kind"] == "word", "落库内容正确",
          f"kind={hi['kind']} off={hi['start_offset']}~{hi['end_offset']}")
    check(bool(hi["context_before"]) or bool(hi["context_after"]), "上下文已保存")

    h1b = c.post(f"/api/articles/{aid}/highlights", json={
        "block_id": target["id"], "start_offset": ws, "end_offset": we,
        "surface": word,
    })
    check(h1b.json()["id"] == hi["id"], "重复划同区间幂等（不堆记录）")

    # 选段：取一个完整句子
    sm = _re.search(r"[A-Z][^.!?]{60,240}[.!?]", text)
    seg = sm.group(0) if sm else text[:200]
    seg_off = sm.start() if sm else 0
    h2 = c.post(f"/api/articles/{aid}/highlights", json={
        "block_id": target["id"],
        "start_offset": seg_off,
        "end_offset": seg_off + len(seg),
        "surface": seg,
    })
    check(h2.status_code == 201, "创建选段痕迹", f"len={len(seg)}")
    seg_h = h2.json() if h2.status_code == 201 else {}
    check(seg_h.get("kind") in ("sentence", "phrase", "paragraph"),
          "选区类别自动推断", f"kind={seg_h.get('kind')}")

    lst = c.get(f"/api/articles/{aid}/highlights").json()
    check(lst["total"] >= 2, "痕迹列表可读", f"total={lst['total']}")

    patched = c.patch(f"/api/highlights/{hi['id']}", json={"note": "验收备注"}).json()
    check(patched["note"] == "验收备注", "备注可更新")

    bad = c.post(f"/api/articles/{aid}/highlights", json={
        "block_id": 99999999, "start_offset": 0, "end_offset": 5})
    check(bad.status_code == 422, "非法块被拒", f"HTTP {bad.status_code}")
    oob = c.post(f"/api/articles/{aid}/highlights", json={
        "block_id": target["id"], "start_offset": 0, "end_offset": 10**6})
    check(oob.status_code == 422, "越界偏移被拒", f"HTTP {oob.status_code}")

    # ------------------------------------------------------------ D 划词查词典
    head("[D] 划词查词典（M10 起本地化，不再经过模型）")
    print("  - 这里原来测的「dict 任务」已下线：划词改查本地词典库，不再产生")
    print("    ai_logs 流水，也不再由模型给释义。音标覆盖、变形词兜底、词形回退")
    print("    这些断言（红线 3）现由 scripts/smoke_m10.py 的 A/C 两组覆盖，")
    print("    它拿 ipa-dict 文件与 stardict.db **逐字比对**，比在这里比对接口更强。")

    # ------------------------------------------------------------ E 选段解析
    head("[E] 选段【解析】（核心功能：译文 + 主干/附加成分 + 语法要点）")
    if args.offline or not seg_h:
        print("  (跳过)")
    else:
        # force=True：这一段上一轮跑过就已经在缓存里了，走缓存的话
        # 「结果有没有真的落进清单」这条断言就永远验不到（缓存路径的 enrolled
        # 是 None，见 services/ai.run）。强制真调一次，让每轮验收都覆盖这条链路。
        r = c.post("/api/ai/explain", json={
            "article_id": aid, "text": seg, "block_id": target["id"],
            "highlight_id": seg_h.get("id"), "force": True,
        })
        check(r.status_code == 200, "解析调用成功", f"HTTP {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            got = body["result"]
            check(body["source"] == "llm", "force 绕过缓存，本次真调用了模型",
                  str(body["source"]))
            print(f"    → 译文: {got['translation'][:70]}")
            print(f"    → 主干: {got['skeleton'][:70]}")
            print(f"    → 附加成分 {len(got['parts'])} 条 / 短语 {len(got['key_phrases'])} 条"
                  f" / 语法要点 {len(got['grammar_points'])} 条 / 难度 {got['difficulty']}")
            check(bool(got["translation"]), "有译文")
            check(bool(got["skeleton"]) or bool(got["parts"]),
                  "有主干或附加成分拆解（长难句的重点）")
            check(all(p.get("text") and p.get("attach") for p in got["parts"]),
                  "每条附加成分都写清了挂靠位置")
            check(isinstance(got["grammar_points"], list)
                  and all(p.get("explain_cn") for p in got["grammar_points"]),
                  "语法要点条目都有讲解（清单的 NOT NULL 约束）")
            # 入册走的是 _post_persist：fresh 调用必须回执一个 points 计数。
            # 条数是否为 0 由内容决定（这段可能确实没有可复用的结构），
            # 「至少 1 条并真的落库」由 smoke_m4.py [F] 用多段候选兜住。
            check(isinstance(body.get("enrolled"), dict) and "points" in body["enrolled"],
                  "解析结果走了语法要点入册路径", str(body.get("enrolled")))

    # ------------------------------------------------------------ F 复用与旧端点
    head("[F] 换划词复用同一段结果（需求 5）+ 旧端点下线")
    if args.offline or not seg_h:
        print("  (--offline 跳过)")
    else:
        # E 段是**带 highlight_id** 解析的；这里**不带** highlight_id 再问同一段，
        # 走的是「换了划词、内容相同」的复用路径：结果照旧、补一条 source=cache
        # 的流水 —— 这正是 stats.cache_hits 的来源，也是需求 5 要的完整痕迹。
        # block_id 必须给：上下文也是缓存键的一部分（input_hash 带 context），
        # 少给这一项算出来就是另一个 hash，会白烧一次调用（这里踩过一次）。
        rr = c.post("/api/ai/explain",
                    json={"article_id": aid, "text": seg, "block_id": target["id"]})
        check(rr.status_code == 200, "复用路径调用成功", f"HTTP {rr.status_code}")
        if rr.status_code == 200:
            rb = rr.json()
            check(rb["source"] == "cache" and rb["tokens_out"] == 0,
                  "内容相同走复用，不烧 token",
                  f"source={rb['source']} tokens={rb['tokens_out']}")
            check(isinstance(rb.get("enrolled"), dict) and "points" in rb["enrolled"],
                  "复用路径也走一遍入册（清单不因缓存断档）", str(rb.get("enrolled")))

    for path in ("/api/ai/translate", "/api/ai/grammar"):
        rr = c.post(path, json={"article_id": aid, "text": seg})
        # Starlette 对未注册路径给 404；若将来误注册成别的动词则是 405。
        check(rr.status_code in (404, 405), f"{path} 不再存在", f"HTTP {rr.status_code}")

    # ------------------------------------------------------------ G 流水与统计
    head("[G] AI 流水（红线 4）")
    logs = c.get("/api/ai/logs", params={"article_id": aid, "limit": 200}).json()
    check(logs["total"] >= (0 if args.offline else 4),
          "该文章的 AI 记录齐全", f"total={logs['total']}")
    bad_json = [l for l in logs["items"] if not isinstance(l["result"], dict)]
    check(not bad_json, "每条 output_json 均可解析")
    types = sorted({l["prompt_type"] for l in logs["items"]})
    print(f"    → 任务类型: {types}")
    all_versions = sorted({l["prompt_version"] for l in logs["items"]})
    print(f"    → 库里出现过的版本: {all_versions}（历史版本保留是预期行为）")
    new_rows = [l for l in logs["items"] if l["id"] > log_id_before]
    if args.offline:
        # --offline 不调模型 ⇒ 本来就不会有本次新增的流水，
        # 「新流水都带当前版本」这条无从谈起（不是失败）
        print("  (- 本次没有新流水：--offline 跳过了所有模型调用)")
    else:
        check(
            bool(new_rows) and all(l["prompt_version"] == cur_version for l in new_rows),
            "本次新增的流水都带当前 prompt 版本",
            f"{len(new_rows)} 条，期望全为 {cur_version}，实得 "
            f"{sorted({l['prompt_version'] for l in new_rows})}",
        )
    # 空结果不许算成功（M3 冷启动实测：deepseek-flash 回过 {"lemma": "straight"}，
    # 合法 JSON 但内容全空，被当成成功结果写进库并进了缓存 → 空卡片且无法自愈）
    empty_dict = [
        l for l in logs["items"]
        if l["prompt_version"] == cur_version
        and l["prompt_type"] == "dict"
        and not (l["result"].get("meaning_cn") or l["result"].get("meaning_en"))
    ]
    check(not empty_dict, "当前版本的 dict 流水里没有空结果",
          f"{len(empty_dict)} 条空" if empty_dict else "0 条空")
    check(all(l["model"] for l in logs["items"]), "每条都记录了模型名")
    for l in logs["items"][:3]:
        print(f"    → #{l['id']} {l['prompt_type']:<9} {l['source']:<6} "
              f"{l['tokens_in']}+{l['tokens_out']} tok  {l['latency_ms']}ms")

    st = c.get("/api/ai/stats").json()
    print(f"    → stats: {json.dumps(st, ensure_ascii=False)}")
    check(st["cache_hits"] >= (0 if args.offline else 1), "统计里有缓存命中",
          f"hits={st['cache_hits']}")
    check(st["highlights"] >= 2, "统计里有划词数", f"{st['highlights']}")

    # ------------------------------------------------------------ H 检索
    head("[H] AI 结果可检索（FTS）")
    fts = c.get("/api/ai/logs", params={"limit": 5}).json()
    check(fts["total"] > 0, "全局流水可读", f"total={fts['total']}")

    # ------------------------------------------------------------ I 删痕迹不删历史
    head("[I] 删痕迹不删 AI 历史")
    if not args.offline:
        before = c.get("/api/ai/logs", params={"limit": 500}).json()["total"]
        dv = c.delete(f"/api/highlights/{hi['id']}")
        after = c.get("/api/ai/logs", params={"limit": 500}).json()["total"]
        check(dv.status_code == 204, "痕迹删除成功")
        check(after == before, "AI 历史未被连带删除", f"{before} → {after}")

    # ------------------------------------------------------------ J 删除解析记录
    # 划词是「选中即翻译」，误操作天生难免，所以记录要能删。
    # 只删本脚本自己刚产生的那些行（id > log_id_before），不碰用户的历史。
    head("[J] 删除解析记录（误操作清理）")
    if not args.offline:
        mine = [
            l for l in c.get("/api/ai/logs", params={"article_id": aid, "limit": 200}).json()["items"]
            if l["id"] > log_id_before
        ]
        if not mine:
            print("    （本次没有新产生的记录，跳过）")
        else:
            victim = mine[0]
            total_before = c.get("/api/ai/logs", params={"article_id": aid, "limit": 200}).json()["total"]
            stats_before = c.get("/api/ai/stats").json()["ai_logs"]
            d = c.delete(f"/api/ai/logs/{victim['id']}")
            check(d.status_code == 204, "删掉一条解析记录",
                  f"#{victim['id']} {victim['prompt_type']}")
            after = c.get("/api/ai/logs", params={"article_id": aid, "limit": 200}).json()
            check(all(l["id"] != victim["id"] for l in after["items"]), "记录从列表消失")
            check(after["total"] == total_before - 1, "总数同步减一",
                  f"{total_before} → {after['total']}")
            check(c.get("/api/ai/stats").json()["ai_logs"] == stats_before - 1, "统计同步减一")
            check(c.delete(f"/api/ai/logs/{victim['id']}").status_code == 404, "重复删除返回 404")
            check(_fts_ai_rows(victim["id"]) == 0, "FTS 索引无残留（成对删除）")

    # ------------------------------------------------------------ 汇总
    total = passed + len(failed)
    print("\n" + "=" * 60)
    print(f"M3 后端验收：{passed}/{total} 通过")
    if failed:
        print("失败项：")
        for f in failed:
            print(f"  - {f}")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
