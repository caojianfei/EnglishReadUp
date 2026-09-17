#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""准备两份离线词典数据 —— 划词查词的全部数据来源。

    ECDICT    —— 释义 / 词性 / 考纲标签 / 词频。官方预编译 SQLite，收词 340 万。
    ipa-dict  —— 英美双音标（美 GA / 英 RP），两个 TSV 合计约 4.6 MiB。

为什么还需要这个脚本：
    服务启动时已经会自动补齐缺失的数据（`app/services/dictdata.py`，后台线程），
    所以日常**不需要**再手工跑它。这个脚本留着的价值是另外两件事：

        --verify   只体检已装好的数据（不联网），打印索引构成、样例词条、
                   考纲标签分布、ipa 命中率 —— 排障时看这些
        --force    全部重下（怀疑数据坏了、想换一份干净的时候）

下载 / 解压 / 落盘那一套实现**只有一份**，在本脚本与启动自检之间共用
（`services/dictdata.py`）：两个入口各写一套，迟早会出现「脚本装得对、服务装不对」。

为什么不导入/转换：
    ECDICT 官方就发预编译 SQLite（`ecdict-sqlite-28.zip`），解压即用，不必再从
    CSV 导入；ipa-dict 只有 TSV，但两个文件才 4.6 MiB，服务端懒加载进内存即可。
    多一层导入就多一个需要同步的产物，不划算。

为什么这里可以放心做全表 COUNT：
    这是一次性的准备脚本，跑一次几十秒；**服务端绝不能这么干** ——
    `/api/health` 里刻意不做 COUNT(*)，3.4M 行会把健康检查拖垮。

用法（工作目录：backend/）：
    python scripts/fetch_dicts.py             # 缺什么装什么，已有的跳过
    python scripts/fetch_dicts.py --force     # 全部重下
    python scripts/fetch_dicts.py --verify    # 只体检已装好的数据，不联网
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services import dictdata  # noqa: E402

IPA_LOCAL = dictdata.IPA_LOCAL

# 考纲标签。ECDICT 的 tag 列是空格分隔的多值，这里按「包含」统计。
# worth 规则要照真实分布定稿，所以把它打出来（见末尾 _report_tags）。
EXAM_TAGS = ("zk", "gk", "cet4", "cet6", "ky", "toefl", "ielts", "gre")

# 体检用的样例词：专名 / 变形词 / 连字符 / 短语各来一个，覆盖几种查询路径。
SAMPLES = ("resilient", "preserved", "Beijing", "state-of-the-art", "run", "the")


# ---------------------------------------------------------------- 下载进度

_last_desc = ""


def _progress(desc: str, got: int, total: int) -> None:
    """把服务层的进度回调打成一行终端输出（下载类输出会原地刷新）。"""
    global _last_desc
    if desc != _last_desc:
        if _last_desc:
            print()
        _last_desc = desc
    if total:
        pct = got * 100 // total
        print(f"\r  {desc}: {pct:3d}%  {got / 1048576:.1f}/{total / 1048576:.1f} MiB", end="")
    elif got:
        print(f"\r  {desc}: {got / 1048576:.1f} MiB", end="")
    else:
        # 阶段提示（「解压中…」）：换一行，别把百分比那行覆盖掉
        print(f"\r  {desc}", end="")


# ---------------------------------------------------------------- 体检
#
# 下面全部是**只读体检**，不参与安装 —— 安装逻辑在 services/dictdata.py。


def _ensure_word_index(conn: sqlite3.Connection) -> None:
    """确保 `word` 上有索引，并把索引构成打出来。

    一定要打出来：**查询能否用上索引取决于索引的列与排序规则**。
    官方这份库的 `word` 是 UNIQUE 且声明成 NOCASE，所以
    `WHERE word = ?` / `... COLLATE NOCASE` 都能等值命中索引，而
    `WHERE lower(word) = ?` 会退化成 340 万行全表扫描（每次查词好几百毫秒）——
    业务侧必须靠「精确 → sw 去符号」这样一串**等值查询**去命中索引。

    准备阶段是**可写**连接，补得成索引；运行时是 `mode=ro`，补不了。
    """
    names = [r["name"] for r in conn.execute("PRAGMA index_list('stardict')")]
    cols: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for info in conn.execute("PRAGMA index_list('stardict')"):
        cols[info["name"]] = (
            bool(info["unique"]),
            tuple(x["name"] for x in conn.execute(f"PRAGMA index_info('{info['name']}')")),
        )

    print("  索引：")
    for name in names:
        uniq, cs = cols[name]
        print(f"    {name:<28} {'UNIQUE ' if uniq else ''}{', '.join(cs)}")

    if dictdata.ensure_word_index(conn):
        print("    → word 可直接等值命中索引，无需补建")
        return
    print("    → 没有覆盖 word 的索引，已补建")


def _check_ecdict() -> None:
    dest = settings.ecdict_file
    if not dest.is_file():
        print(f"[ECDICT] 未安装（{dest}）")
        return
    conn = sqlite3.connect(dest)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_word_index(conn)
        total = conn.execute("SELECT COUNT(*) c FROM stardict").fetchone()["c"]
        print(f"  词条数：{total:,}")

        print("  样例：")
        for w in SAMPLES:
            row = conn.execute(
                "SELECT word, phonetic, pos, tag, collins, oxford, exchange,"
                " substr(COALESCE(translation,''), 1, 40) AS tr"
                " FROM stardict WHERE word = ? COLLATE NOCASE LIMIT 1",
                (w,),
            ).fetchone()
            if row is None:
                print(f"    {w:<18} 未收录")
                continue
            print(
                f"    {row['word']:<18} ph={_show(row['phonetic']):<20}"
                f" pos={_show(row['pos']):<12} tag={_show(row['tag']):<18}"
                f" collins={row['collins']} oxford={row['oxford']}"
                f" ex={_show(row['exchange'])}"
            )
            print(f"      {row['tr']}")

        _report_tags(conn)
        _report_pos(conn)
        _report_coverage(conn)
    finally:
        conn.close()


def _report_tags(conn: sqlite3.Connection) -> None:
    """打印考纲标签分布 —— `worth` 规则要照真实取值为准，不能照抄文档里的例子。

    实测这份 1.0.28 的库里带标签的词只有 1.5 万左右（占 340 万的 0.4%），
    所以「按标签判值得记」只能覆盖极小一部分，必须有别的口径兜底。
    """
    print("  标签分布（LIKE 扫描，仅供定 worth 规则）：")
    for tag in EXAM_TAGS:
        n = conn.execute(
            "SELECT COUNT(*) c FROM stardict WHERE tag LIKE ?", (f"%{tag}%",)
        ).fetchone()["c"]
        print(f"    {tag:<8} {n:,}")
    n = conn.execute("SELECT COUNT(*) c FROM stardict WHERE COALESCE(tag,'') != ''").fetchone()["c"]
    print(f"    {'any':<8} {n:,}")


def _report_coverage(conn: sqlite3.Connection) -> None:
    """打印几个关键列的覆盖率。

    这直接决定两件事：
      1. worth（值不值得记）能靠什么判 —— 实测带考纲标签的只有 1.5 万词，
         所以必须有别的兜底口径；
      2. ECDICT 音标兜底路径会被触发得多频繁 —— 覆盖率越低，越依赖 ipa-dict。
    """
    total = conn.execute("SELECT COUNT(*) c FROM stardict").fetchone()["c"]
    cols = (
        ("音标", "COALESCE(phonetic,'') != ''"),
        ("词性(pos)", "COALESCE(pos,'') != ''"),
        ("柯林斯星级", "COALESCE(collins,0) > 0"),
        ("牛津三千", "COALESCE(oxford,0) = 1"),
        ("BNC 词频", "COALESCE(CAST(bnc AS TEXT),'') != ''"),
        ("当代词频 frq", "COALESCE(CAST(frq AS TEXT),'') != ''"),
        ("考纲标签", "COALESCE(tag,'') != ''"),
        ("中文释义", "COALESCE(translation,'') != ''"),
    )
    print("  列覆盖率：")
    for label, cond in cols:
        n = conn.execute(f"SELECT COUNT(*) c FROM stardict WHERE {cond}").fetchone()["c"]
        print(f"    {label:<14} {n:>9,}  {n * 100 // max(total, 1):>3d}%")
    # 词频列的真身：格式不确定（可能是文本也可能是数字），抽几个最小的看一眼。
    rows = conn.execute(
        "SELECT word, frq, bnc FROM stardict"
        " WHERE COALESCE(CAST(frq AS TEXT),'') != ''"
        " ORDER BY CAST(frq AS INTEGER) ASC LIMIT 5"
    ).fetchall()
    if rows:
        print("    frq 最小的几个：" + " / ".join(f"{r['word']}({r['frq']})" for r in rows))


def _report_pos(conn: sqlite3.Connection) -> None:
    """打印 pos 首字母分布 + 样例词。

    ECDICT 的 pos 是 BNC 词性标注体系（`n:46/v:54` = 作名词 46% / 动词 54%），
    文档里只举了 n / v 两个例子，其余字母的含义得照真实数据反推 —— 业务侧要把
    它翻成 `n. / v. / adj.` 这类展示形态（见 services/ecdict.py）。
    """
    print("  词性首字母分布（用于确定 BNC → 展示形态的映射）：")
    rows = conn.execute(
        "SELECT substr(pos,1,1) AS k, COUNT(*) c FROM stardict"
        " WHERE COALESCE(pos,'') != '' GROUP BY k ORDER BY c DESC LIMIT 15"
    ).fetchall()
    for r in rows:
        samples = [
            x["word"] for x in conn.execute(
                "SELECT word FROM stardict WHERE pos LIKE ? LIMIT 4", (f"{r['k']}:%",)
            )
        ]
        print(f"    {r['k']}: {r['c']:>9,}   {' / '.join(samples)}")


# ---------------------------------------------------------------- ipa-dict

def _load_ipa() -> dict[str, dict[str, str]]:
    """读两个 TSV，返回 `{word: {accent: ipa}}`。格式：`词<TAB>/音标/`。"""
    out: dict[str, dict[str, str]] = {}
    for accent, local in IPA_LOCAL.items():
        path = settings.ipa_dir / local
        if not path.is_file():
            print(f"[ipa-dict] {local} 缺失，跳过")
            continue
        n = 0
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    continue
                word = parts[0].strip()
                # 一个词可能有多个读音，逗号分隔；只取第一个 —— 与查词结果的
                # 单值口径一致，也避免词卡上出现一长串。
                ipa = parts[1].split(",")[0].strip()
                if not word or not ipa:
                    continue
                out.setdefault(word.lower(), {})[accent] = ipa
                n += 1
        print(f"  {local}: {n:,} 条")
    return out


def _check_ipa() -> None:
    if not settings.ipa_dir.is_dir():
        print(f"[ipa-dict] 未安装（{settings.ipa_dir}）")
        return
    table = _load_ipa()
    print(f"  合计唯一条目：{len(table):,}")

    # 命中率：拿 ECDICT 里两个口径的词各抽一批，看看 ipa-dict 到底盖住了多少。
    # 这决定「缺一边就只显示一边」与 ECDICT 兜底路径会被触发得多频繁。
    #
    # 取样口径很关键：不能拿全库随机当「常见词」—— ECDICT 收了 340 万条，
    # 绝大多数是专名、生僻词和短语，随机抽出来的东西读者根本遇不上。
    # 用「柯林斯星级」和「牛津三千」这两个**面向学习者**的标注更贴近真实场景。
    dest = settings.ecdict_file
    if not dest.is_file():
        return
    conn = sqlite3.connect(dest)
    conn.row_factory = sqlite3.Row
    try:
        for label, sql in (
            ("柯林斯星级词（collins>0）",
             "SELECT word FROM stardict WHERE COALESCE(collins,0) > 0 LIMIT 2000"),
            ("牛津核心词（oxford=1）",
             "SELECT word FROM stardict WHERE COALESCE(oxford,0) = 1 LIMIT 2000"),
            ("全库随机（多为生僻词/专名）",
             "SELECT word FROM stardict WHERE length(word) > 2 ORDER BY RANDOM() LIMIT 2000"),
        ):
            words = [r["word"] for r in conn.execute(sql)]
            hit_uk = sum(1 for w in words if "uk" in table.get(w.lower(), {}))
            hit_us = sum(1 for w in words if "us" in table.get(w.lower(), {}))
            hit_both = sum(
                1 for w in words
                if {"uk", "us"} <= set(table.get(w.lower(), {}))
            )
            n = len(words) or 1
            print(f"  {label} 命中率：英 {hit_uk * 100 // n}% / 美 {hit_us * 100 // n}%"
                  f" / 双 {hit_both * 100 // n}%（{n} 词样本）")
    finally:
        conn.close()


# ---------------------------------------------------------------- 入口

def _show(text: str | None) -> str:
    """给控制台打印用的安全文本。

    ECDICT 的音标里混着西里尔字母 `ә`（老式英标把 `ə` 写成了它，实测确实存在），
    而 Windows 控制台默认是 GBK —— 直接 print 会抛 UnicodeEncodeError 把整个
    脚本打断。打不出来就退成转义形式（`\\u04d9`），信息不丢，也不动库里的原值。
    """
    if not text:
        return "—"
    enc = sys.stdout.encoding or "utf-8"
    try:
        text.encode(enc)
        return text
    except UnicodeEncodeError:
        return text.encode("unicode_escape").decode("ascii")


def _install(force: bool) -> int:
    """缺什么装什么（--force 则全部重下）。安装实现全在 services/dictdata.py。"""
    todo = dictdata.datasets() if force else dictdata.missing()
    if not todo:
        print("三个文件都在，无需下载（--force 可全部重下）。")
        return 0
    print("待安装：" + "、".join(f"{d['label']}（{d['path']}）" for d in todo) + "\n")
    try:
        dictdata.ensure(force=force, progress=_progress)
    except Exception as e:  # noqa: BLE001
        print(f"\n安装失败：{e}")
        print("可以重跑本脚本；也可以看 /api/health 的 dict.bootstrap（启动自检的状态）")
        return 1
    print()
    for d in dictdata.datasets():
        size = d["path"].stat().st_size / 1048576 if d["path"].is_file() else 0
        print(f"  {d['label']:<12} {size:>9.1f} MiB  {d['path']}")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="准备 ECDICT 与 ipa-dict 离线数据")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--verify", action="store_true", help="只体检已装好的数据，不联网")
    args = ap.parse_args()

    settings.ensure_dirs()
    print(f"数据目录：{settings.data_dir}\n")

    rc = 0 if args.verify else _install(args.force)

    _check_ecdict()
    print()
    _check_ipa()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
