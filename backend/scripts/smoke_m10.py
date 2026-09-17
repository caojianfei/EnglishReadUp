"""M10 后端验收：划词本地化（ECDICT + ipa-dict）与英美双口音。

跑法（后端需已在 8010 运行）：

    python scripts/smoke_m10.py                 # 全套
    python scripts/smoke_m10.py --article-id 2  # 指定文章（默认自动挑一篇）
    python scripts/smoke_m10.py --no-audio      # 跳过真下音频的那组（离线/不想等时用）

验收重点（对应 design 与红线）：

  红线 3 —— 词条的每一个字都来自**本地词典库**：`/api/dict/{word}` 返回的音标与
            `data/ipa_en_*.txt` 里那一行**逐字相等**，释义能在 `stardict.db` 里查到。
            这条断言是拿文件本身比对的，不是拿接口自己比自己。
  需求 5 —— 划词自动入册；同一篇里重复查同一个词**不重复计数**（`seen_count`）。
  需求 6 —— 汇总的「划过但还没解析」**不含单词**：划中那一刻就查完了，没有待办。
  双口音 —— 两个槽位彼此独立：一侧 `missing` 不影响另一侧；音频**点击才下**。

脚本会造两条痕迹（一条 word、一条 phrase）与一次入册用于断言，**跑完自己清干净**。
它不会修改任何文章内容，也不调模型（除音频组外不出网）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8010"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

passed = 0
failed: list[str] = []
created_highlights: list[tuple[int, int]] = []   # (article_id, highlight_id)
created_vocab: list[int] = []


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


def skip(t: str) -> None:
    print(f"  - {t}")


# ---------------------------------------------------------------- 本地数据的真身

def load_ipa(accent: str) -> dict[str, str]:
    """读 ipa-dict 原始文件，返回 `{词: 第一条音标}` —— 红线 3 的比对基准。

    刻意不用后端接口：断言必须是「接口 == 文件」，而不是「接口 == 接口」。
    """
    path = DATA / f"ipa_en_{accent}.txt"
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                out.setdefault(parts[0].strip().lower(), parts[1].split(",")[0].strip())
    return out


def ecdict_lookup(words: list[str]) -> dict[str, dict]:
    """直接查 stardict.db，返回 `{词: {列: 值}}`。只读，只为断言用。"""
    import sqlite3

    path = DATA / "stardict.db"
    if not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out = {}
        for w in words:
            row = conn.execute(
                "SELECT word, phonetic, translation, pos, tag, exchange"
                " FROM stardict WHERE word = ? LIMIT 1",
                (w,),
            ).fetchone()
            if row is not None:
                out[w] = dict(row)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- 组

def group_a_contract(c: httpx.Client) -> dict:
    """A 词典数据就位 + 词条契约。返回 `resilient` 的词条供后面复用。"""
    head("[A] 本地词典数据与词条契约")
    h = c.get("/api/health", timeout=20).json()
    d = h.get("dict") or {}
    ec = d.get("ecdict") or {}
    check(ec.get("exists") is True, "health 里 ECDICT 在位", str(ec.get("path")))
    check(int(ec.get("bytes") or 0) > 500 * 1024 * 1024, "ECDICT 体积合理", f"{int(ec.get('bytes') or 0) // (1024 * 1024)} MB")
    files = (d.get("ipa") or {}).get("files") or {}
    check(bool(files.get("us")) and bool(files.get("uk")), "ipa-dict 英美两个文件都在", str(files))
    # 健康检查**刻意不报词条数**：340 万行的 COUNT(*) 会把健康检查拖成秒级
    check("total" not in ec, "health 不做 COUNT(*)（不把健康检查拖慢）")

    entry = c.get("/api/dict/resilient", timeout=30).json()
    check(entry.get("source") == "ecdict", "查得到词条", str(entry.get("source")))
    check(entry.get("headword") == "resilient", "headword 是词典里的真实拼写", str(entry.get("headword")))
    check(entry.get("lemma") == "resilient", "lemma 取自词典", str(entry.get("lemma")))
    check(bool(entry.get("pos")), "有词性", str(entry.get("pos")))
    check(bool(entry.get("meaning_cn")), "有中文释义", (entry.get("meaning_cn") or "")[:24])
    check(entry.get("worth") == 1, "gre 词判为值得记", str(entry.get("worth")))

    # 红线 3：音标必须与 ipa-dict 文件里那一行逐字相等
    for accent in ("uk", "us"):
        table = load_ipa(accent)
        want = table.get("resilient")
        got = entry.get(f"phonetic_{accent}")
        if want is None:
            skip(f"ipa_en_{accent}.txt 里没有 resilient，跳过逐字比对")
        else:
            check(got == want, f"{accent} 音标与 ipa-dict 文件逐字相等", f"{got!r}")
    check(entry.get("phonetic_source") == "ipa", "音标来源标为 ipa", str(entry.get("phonetic_source")))

    # 释义必须能在本地库里查到（只比前 12 字，接口会截断长释义）
    rows = ecdict_lookup(["resilient"])
    raw = (rows.get("resilient") or {}).get("translation") or ""
    check(
        bool(raw) and raw.strip()[:12] in (entry.get("meaning_cn") or ""),
        "中文释义与 stardict.db 一致",
        (entry.get("meaning_cn") or "")[:20],
    )

    audios = entry.get("audios") or {}
    check(set(audios) == {"uk", "us"}, "两个发音槽都在", str(sorted(audios)))
    check(
        all((audios[a].get("status") in ("ok", "pending", "missing")) for a in audios),
        "槽位状态取值合法",
        str({a: audios[a].get("status") for a in audios}),
    )
    check(
        all("/audio?accent=" in (audios[a].get("url") or "") for a in audios),
        "播放地址带 accent 参数（本机地址，非外链）",
        str(audios["uk"].get("url")),
    )

    # 含斜杠的词走 query 形式
    r = c.get("/api/dict", params={"word": "and/or"}, timeout=30)
    check(r.status_code == 200, "含斜杠的词走 query 形式", f"HTTP {r.status_code}")
    return entry


def group_b_worth(c: httpx.Client) -> None:
    """B `worth` 由本地规则推导（考纲标签 → 牛津/柯林斯 → 词频）。"""
    head("[B] worth 本地规则")
    cases = [
        ("the", 0, "zk gk"),
        ("run", 0, "zk gk"),
        ("make", 0, "zk gk ielts（基础标签优先于考试标签）"),
        ("resilient", 1, "gre"),
        ("kaleidoscope", 1, "gre"),
    ]
    for word, want, why in cases:
        got = c.get(f"/api/dict/{word}", timeout=30).json()
        check(got.get("worth") == want, f"{word} → worth={want}", f"{why}；实得 {got.get('worth')} tags={got.get('tags')!r}")


def group_c_fallback(c: httpx.Client) -> None:
    """C 兜底与降级：变形词、单侧音标、未收录、库缺失。"""
    head("[C] 兜底与降级")

    # 变形词：lemma 来自 ECDICT 的 exchange（0:原形），纯本地
    got = c.get("/api/dict/preserved", timeout=30).json()
    check(got.get("lemma") == "preserve", "变形词的 lemma 取自 exchange", str(got.get("lemma")))
    check(got.get("source") == "ecdict", "变形词自己也有词条（释义），不必回查", str(got.get("source")))

    # 单侧音标：ipa-dict 对专名常常只收美音 —— 只显示一侧，但**两个喇叭都要给**
    for word in ("Beijing", "Thailand"):
        got = c.get(f"/api/dict/{word}", timeout=30).json()
        if got.get("source") != "ecdict":
            skip(f"{word} 未收录，跳过单侧音标断言")
            continue
        uk, us = got.get("phonetic_uk"), got.get("phonetic_us")
        check(bool(uk) != bool(us), f"{word} 只有一侧音标", f"uk={uk!r} us={us!r}")
        check(
            set((got.get("audios") or {})) == {"uk", "us"},
            f"{word} 音标缺一侧但两个喇叭都在（录音与音标是两件事）",
            str(sorted(got.get("audios") or {})),
        )

    # 未收录
    got = c.get("/api/dict/zzzqqqxxm10probe", timeout=30).json()
    check(got.get("source") == "missing", "未收录词给出 missing", str(got.get("source")))
    check(got.get("phonetic") is None and got.get("audios") == {}, "未收录词不给音标与喇叭")
    check(got.get("meaning_cn") == "", "未收录词不给释义")

    # phonetic_source 的取值只在三个里
    for word in ("resilient", "inconsequent", "zzzqqqxxm10probe"):
        got = c.get(f"/api/dict/{word}", timeout=30).json()
        check(
            got.get("phonetic_source") in ("ipa", "ecdict", "missing"),
            f"{word} 的 phonetic_source 合法",
            str(got.get("phonetic_source")),
        )


def group_d_enroll(c: httpx.Client, article_id: int, word: str, sentence: str) -> None:
    """D 自动入册与幂等（`seen_count` 只在同篇第一次遇见时涨）。"""
    head("[D] 划词自动入册")
    qs = {"article_id": article_id, "sentence": sentence}

    first = c.get(f"/api/dict/{word}", params=qs, timeout=30).json()
    enr = first.get("enrolled") or {}
    check(enr.get("enrolled") is True, "查词即入册", str(enr))
    vid = enr.get("vocab_id")
    if enr.get("created") is True and vid:
        created_vocab.append(int(vid))
    # 入册的是 **lemma**（划 visitors 建成 visitor），所以后面按 lemma 查。
    # 拿划到的词形去 `?q=` 搜会搜不到 —— 那是搜索口径，不是数据问题。
    lemma = str(enr.get("lemma") or word)

    def seen_now() -> int | None:
        items = c.get("/api/vocab", params={"q": lemma, "limit": 20}, timeout=30).json()["items"]
        hit = next((x for x in items if x["lemma"].lower() == lemma.lower()), None)
        return hit["seen_count"] if hit else None

    before = seen_now()
    second = c.get(f"/api/dict/{word}", params=qs, timeout=30).json()
    after = seen_now()
    check((second.get("enrolled") or {}).get("created") is False, "重复查不重复创建")
    check(
        before is not None and after == before,
        "同篇重复查不涨 seen_count",
        f"{lemma}: {before} → {after}",
    )

    # 生词本条目必须带两个发音槽（列表页要画两个喇叭）
    items = c.get("/api/vocab", params={"q": lemma, "limit": 20}, timeout=30).json()["items"]
    hit = next((x for x in items if x["lemma"].lower() == lemma.lower()), None)
    if hit:
        check(set(hit.get("audios") or {}) == {"uk", "us"}, "生词本条目带英美两个槽", str(sorted(hit.get("audios") or {})))
        check(bool(hit.get("phonetic")), "生词本有主音标（美音优先）", str(hit.get("phonetic")))


def group_e_audio(c: httpx.Client, word: str) -> None:
    """E 发音：点击才下载、落盘、双槽独立。"""
    head("[E] 发音（点击式下载）")
    before = c.get("/api/health", timeout=20).json().get("audio") or {}
    cached_before = int(before.get("cached") or 0)

    for accent in ("uk", "us"):
        r = c.get(
            f"/api/dict/{word}/audio",
            params={"accent": accent},
            timeout=180,
        )
        # 该口音确认没有录音时 404 是**正确**结果，不当失败
        if r.status_code == 404:
            skip(f"{word}[{accent}] 两个源都没有录音（404 是终态）")
            continue
        check(r.status_code == 200, f"{word}[{accent}] 播放端点给出音频", f"HTTP {r.status_code}")
        if r.status_code != 200:
            continue
        ctype = r.headers.get("content-type") or ""
        check(ctype.startswith("audio/"), f"{word}[{accent}] 返回的是音频类型", ctype)
        check(len(r.content) > 1024, f"{word}[{accent}] 字节数像真音频", f"{len(r.content)} B")
        check(
            "://" not in (r.headers.get("x-audio-origin") or ""),
            f"{word}[{accent}] 溯源只记来源名，不外链",
            str(r.headers.get("x-audio-origin")),
        )

    after = c.get("/api/health", timeout=20).json().get("audio") or {}
    check(
        int(after.get("cached") or 0) >= cached_before,
        "健康检查的 cached 计数只增不减",
        f"{cached_before} → {after.get('cached')}",
    )
    entry = c.get(f"/api/dict/{word}", timeout=30).json()
    slots = entry.get("audios") or {}
    check(
        any((slots.get(a) or {}).get("status") == "ok" for a in ("uk", "us")),
        "下过的那一槽状态变成 ok",
        str({a: (slots.get(a) or {}).get("status") for a in sorted(slots)}),
    )


def group_f_summary(c: httpx.Client, article_id: int) -> None:
    """F 汇总：没有解析记录的痕迹进不了清单（「待解析」不是一种状态）。"""
    head("[F] 汇总页不收纳「划过但还没解析」")
    art = c.get(f"/api/articles/{article_id}", timeout=30).json()
    # 图片块的 text 是 None（它指图像，没有文本），别直接 len()
    blocks = [
        b for b in art["blocks"]
        if b["type"] in ("para", "title") and len(b.get("text") or "") > 60
    ]
    if not blocks:
        skip("这篇没有可用的文本块，跳过")
        return
    blk = blocks[0]
    text: str = blk["text"]

    # 一条 word 痕迹 + 一条 phrase 痕迹 —— 都**没有** ai_logs 挂靠，
    # 是「划过但从没解析过」的孤立痕迹（旧版本会把它们列成待办）。
    m = re.search(r"[A-Za-z]{4,}", text)
    if not m:
        skip("块里没有英文单词，跳过")
        return
    w = c.post(
        f"/api/articles/{article_id}/highlights",
        json={"block_id": blk["id"], "start_offset": m.start(), "end_offset": m.end(),
              "kind": "word", "surface": m.group(0)},
        timeout=30,
    ).json()
    created_highlights.append((article_id, int(w["id"])))
    s0 = min(m.end() + 1, max(0, len(text) - 40))
    ph = c.post(
        f"/api/articles/{article_id}/highlights",
        json={"block_id": blk["id"], "start_offset": s0, "end_offset": min(len(text), s0 + 36),
              "surface": text[s0:s0 + 36]},
        timeout=30,
    ).json()
    created_highlights.append((article_id, int(ph["id"])))

    smy = c.get(f"/api/articles/{article_id}/summary", timeout=30).json()
    check("uncovered" not in smy, "汇总不再有「待解析」一栏", str(sorted(smy)))
    ids = {int(w["id"]), int(ph["id"])}
    check(all(x.get("highlight_id") not in ids for x in smy["grammar"]),
          "孤立痕迹不进语法清单（没有解析就没有条目）",
          f"语法 {len(smy['grammar'])} 条")
    check(all(x.get("highlight_id") not in ids for x in smy["words"]),
          "孤立痕迹不进生词表（单词只在真查过词典后才入册）",
          f"生词 {len(smy['words'])} 条")


# ---------------------------------------------------------------- 收尾

def cleanup(c: httpx.Client) -> None:
    head("[Z] 清理")
    n = 0
    for _article_id, hid in created_highlights:
        try:
            r = c.delete(f"/api/highlights/{hid}", timeout=30)
            n += 1 if r.status_code in (200, 204) else 0
        except Exception:  # noqa: BLE001
            pass
    check(n == len(created_highlights), f"删掉自己造的 {len(created_highlights)} 条痕迹", f"成功 {n}")
    m = 0
    for vid in created_vocab:
        try:
            r = c.delete(f"/api/vocab/{vid}", timeout=30)
            m += 1 if r.status_code in (200, 204) else 0
        except Exception:  # noqa: BLE001
            pass
    if created_vocab:
        check(m == len(created_vocab), f"删掉本次新建的 {len(created_vocab)} 条生词", f"成功 {m}")
    else:
        skip("本次没新建生词（都已存在）")


def main() -> int:
    ap = argparse.ArgumentParser(description="M10 划词本地化验收")
    ap.add_argument("--article-id", type=int, default=None)
    ap.add_argument("--no-audio", action="store_true", help="跳过真下音频的那一组")
    ap.add_argument("--word", default="resilient", help="音频组用哪个词")
    args = ap.parse_args()

    with httpx.Client(base_url=BASE, timeout=60) as c:
        try:
            c.get("/api/health", timeout=10)
        except Exception as e:  # noqa: BLE001
            print(f"后端没有在 {BASE} 上运行：{e}")
            return 2

        group_a_contract(c)
        group_b_worth(c)
        group_c_fallback(c)

        article_id = args.article_id
        if article_id is None:
            items = c.get("/api/articles", params={"limit": 1}, timeout=30).json().get("items") or []
            article_id = int(items[0]["id"]) if items else None
        if article_id is None:
            head("[D][F] 入册与汇总")
            skip("库里还没有文章，跳过（这两组依赖一篇真实文章）")
        else:
            art = c.get(f"/api/articles/{article_id}", timeout=30).json()
            blocks = [b for b in art["blocks"] if len(b.get("text") or "") > 80]
            text = blocks[0]["text"] if blocks else ""
            m = re.search(r"[A-Za-z]{5,}", text)
            word = m.group(0) if m else args.word
            group_d_enroll(c, article_id, word, text[:200])
            group_f_summary(c, article_id)

        if args.no_audio:
            head("[E] 发音")
            skip("--no-audio：跳过真下音频的检查")
        else:
            group_e_audio(c, args.word)

        cleanup(c)

    print(f"\n通过 {passed} 项" + (f"，失败 {len(failed)} 项：{failed}" if failed else "，无失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
