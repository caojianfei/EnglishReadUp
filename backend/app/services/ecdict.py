"""ECDICT 本地词条查询：中文释义 / 词性 / 考纲标签 / 词频。

红线 3 的新落点：**释义与音标都只来自本地词典库**，不由模型生成、也不打外网。
ECDICT 是官方发布的预编译 SQLite（340 万词条，99% 有中文释义），只读连接、按需查。

为什么不再需要缓存表：
    以前有一张 `dict_cache` 缓存 Wiktionary 的解析结果 —— 那是网络请求，值得缓存。
    现在是本地文件、单次查询 0.0x 毫秒，再套一层缓存只是给自己加一个失效维度
    （parser_ver / TTL），所以 M10 起整张表连同读写代码一起去掉了。

查询为什么写成一串等值查询，而不是一句 LIKE / lower()：
    实测（`scripts/fetch_dicts.py --verify` 会把索引构成打出来）：

        WHERE word = ?                   0.0 ms   走 stardict_2 索引
        WHERE word = ? COLLATE NOCASE    0.0 ms   同样走索引
        WHERE lower(word) = ?          458.7 ms   ★ 全表扫描（340 万行）
        WHERE sw = ?                     0.2 ms   走 stardict_3(sw, word) 索引

    官方这份库的 `word` 列**本身就声明成 NOCASE**，所以 `=` 天生大小写不敏感
    （`beijing` 能查到 `Beijing`）。也就是说 lower()/LIKE 一点好处都没有，
    却会让每次划词从 0 毫秒变成半秒 —— 别"顺手兼容一下大小写"。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from ..config import settings

log = logging.getLogger("english.ecdict")

# BNC 词性标注体系的首字母 → 展示形态。
# 上游只举了 n / v 两个例子，其余字母是照真实数据反推出来的
# （`scripts/fetch_dicts.py` 的词性报告会打印分布与样例词）：
#   n: abacus/A-bomb    j: abandoned       v: abase        r: aback
#   m: billion/dozen    i: aboard/about    p: anybody      c: although/and
#   d: few/former       a: a/every/her     u: aah/a-ha（感叹词）  t: tae
POS_MAP = {
    "n": "n.",
    "v": "v.",
    "j": "adj.",
    "r": "adv.",
    "m": "num.",
    "i": "prep.",
    "p": "pron.",
    "c": "conj.",
    "d": "det.",
    "a": "art.",
    "u": "int.",
    "t": "abbr.",
}

# 释义前缀里的词性标记：ECDICT 的中文释义常写成「n. 跑, 赛跑…」。
# 为什么要多这一条：`pos` 列实测只覆盖 1%（52,527/3,402,564），而释义本身
# 99% 都有值，前缀里往往就带着词性。两者都取不到就留空 —— 词卡会把整条释义
# 显示出来，标记本身仍然看得见，不必为它编一个值。
_POS_PREFIX = re.compile(r"^\s*\[?([A-Za-z]{1,6})\.\]?\s")
_PREFIX_POS_MAP = {
    "n": "n.",
    "un": "n.",          # 数据里对不可数名词的写法
    "v": "v.",
    "vt": "vt.",
    "vi": "vi.",
    "a": "adj.",
    "adj": "adj.",
    "ad": "adv.",
    "adv": "adv.",
    "prep": "prep.",
    "conj": "conj.",
    "pron": "pron.",
    "art": "art.",
    "num": "num.",
    "int": "int.",
    "abbr": "abbr.",
    "pl": "pl.",
    "suf": "suf.",
    "suff": "suf.",
}

# exchange 里的原形：`0:preserve/1:dp` → preserve。
# 变形词自己有词条（所以能查到中文释义），但**往往没有音标** ——
# 实测 `preserved` 的 phonetic 是空的。原形交给调用方去补音标。
_EXCHANGE_LEMMA = re.compile(r"(?:^|/)0:([^/]+)")

# 中文释义 / 英文释义的长度收敛。ECDICT 的释义是多行长文本，原样丢给前端
# 会让一张词卡撑到屏幕外，JSON 也会莫名几 KB。
MAX_MEANING_CHARS = 300
MAX_MEANING_LINES = 4
MAX_DEFINITION_CHARS = 200

BASIC_TAGS = frozenset({"zk", "gk", "cet4"})          # 中考 / 高考 / 四级
EXAM_TAGS = frozenset({"cet6", "ky", "toefl", "ielts", "gre"})
COMMON_FREQ_MAX = 2000                                # 词频排名前 2000 视为基础词


# ---------------------------------------------------------------- 词形归一

def normalize_word(raw: str) -> str:
    """划词常带标点，统一成词典能查的形态。

    这是全库唯一的归一化口径（查词、发音、生词本入册都走它），
    所以放在最底层这一层，`dictionary.py` 直接复用。
    """
    w = raw.strip().strip("\"'“”‘’()[]{}<>,.;:!?—–-… \t\n")
    # 全角字母（一些中文页面里会出现）转半角
    w = "".join(
        chr(ord(c) - 0xFEE0) if "\uff21" <= c <= "\uff5a" else c for c in w
    )
    return w.strip().lower()


def stripword(raw: str) -> str:
    """去符号小写形式 —— 对齐 ECDICT 的 `sw` 列。

    用途是「连字符/撇号怎么断都算同一个词」：`state-of-the-art` 与
    `state of the art` 的 sw 都是 `stateoftheart`。ECDICT 的 `sw` 有索引
    （stardict_3），所以这是条 0.2ms 的兜底路。
    """
    return "".join(c for c in (raw or "") if c.isalnum()).lower()


# ---------------------------------------------------------------- 长文本收敛

def _clip(text: str | None, *, max_chars: int, max_lines: int = 0) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if max_lines > 0:
        lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
        s = "\n".join(lines[:max_lines])
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "…"
    return s


# ---------------------------------------------------------------- pos / lemma / worth

def parse_pos(pos_col: str | None, translation: str | None = None) -> str:
    """词性展示形态。`pos` 列优先（它是按语料统计出来的占比），其次看释义前缀。

    `pos` 的格式是占比：`n:15/v:85` → 取占比最高的那个（run 作动词 85%）。
    """
    raw = (pos_col or "").strip()
    if raw:
        best_letter, best_n = None, -1
        for part in raw.split("/"):
            token = part.split(":")
            if not token or not token[0].strip():
                continue
            letter = token[0].strip().lower()
            try:
                n = int(token[1]) if len(token) > 1 and token[1].strip() else 0
            except ValueError:
                n = 0
            if letter in POS_MAP and n > best_n:
                best_letter, best_n = letter, n
        if best_letter:
            return POS_MAP[best_letter]

    m = _POS_PREFIX.match(translation or "")
    if m:
        return _PREFIX_POS_MAP.get(m.group(1).lower(), "")
    return ""


def lemma_of(exchange: str | None, fallback: str) -> str:
    """从 `exchange` 里取原形；没有就是它自己。"""
    m = _EXCHANGE_LEMMA.search(exchange or "")
    return (m.group(1).strip() if m else "") or fallback


def _worth(tags: str, collins: Any, oxford: Any, frq: Any) -> int:
    """「值不值得记」（0/1）。取代原来 LLM 给的 worth 判定。

    判据全部来自 ECDICT 自带的学习者标注，按信号强度从上往下取第一条命中：

      1. 命中 zk / gk / cet4 → 0    中考、高考、四级词汇：读到这个阶段的人早会了
      2. 命中 cet6 / ky / toefl / ielts / gre → 1    大纲词就是要背的
      3. 牛津三千 或 柯林斯 >= 4 星 → 0    学习者词典的核心高频词
      4. 词频排名在 1~2000 之间 → 0
      5. 其余 → 1（沿用「判不准就填 1，宁可多记不要漏记」的既有立场）

    两条实测得到的注意事项：
      * **1 必须排在 2 前面**：`make` 的标签是 `zk gk ielts`，先看考试标签会把
        它判成"值得记"，但它显然是基础词。基础标签优先才与直觉一致。
      * `frq` 可能是 **0**，那是「没排上名次」的占位值（'hood(0)、-ability(0)），
        不是"最高频"，所以写成 `1 <= frq <= 2000`。
    """
    tagset = {t for t in (tags or "").split() if t}
    if tagset & BASIC_TAGS:
        return 0
    if tagset & EXAM_TAGS:
        return 1
    if _as_int(oxford) == 1 or _as_int(collins) >= 4:
        return 0
    n = _as_int(frq)
    if 1 <= n <= COMMON_FREQ_MAX:
        return 0
    return 1


def _as_int(v: Any) -> int:
    """SQLite 里这些列可能是 INTEGER 也可能是 TEXT（官方 CSV 导入的遗留），别信类型。"""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 连接

def available() -> bool:
    return settings.ecdict_file.is_file()


def status() -> dict[str, Any]:
    """给 /api/health 用的轻量状态。

    **不做 COUNT(*)**：340 万行的计数会把健康检查拖成秒级。词条数由
    `scripts/fetch_dicts.py` 打印（那是一次性的准备脚本，慢一点无所谓）。
    """
    path = settings.ecdict_file
    return {
        "path": str(path),
        "exists": path.is_file(),
        "bytes": path.stat().st_size if path.is_file() else 0,
    }


def _connect() -> sqlite3.Connection | None:
    """按调用开一个只读短连接，和 db.py 的「每操作一个短连接」保持一致。

    只读是靠 URI 的 `mode=ro` 保证的（运行时绝不可能写第三方库文件，也写不进），
    路径要 quote —— 本机路径里可能有空格或中文。
    """
    if not available():
        return None
    uri = "file:" + quote(settings.ecdict_file.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


_COLS = (
    "word, phonetic, translation, definition, pos, collins, oxford, tag, frq, exchange"
)


def _fetch(conn: sqlite3.Connection, word: str) -> sqlite3.Row | None:
    """按优先级取词条。三条都是等值查询，全部命中索引。"""
    for sql, key in (
        ("SELECT " + _COLS + " FROM stardict WHERE word = ? LIMIT 1", word),
        ("SELECT " + _COLS + " FROM stardict WHERE sw = ? LIMIT 1", stripword(word)),
    ):
        if not key:
            continue
        row = conn.execute(sql, (key,)).fetchone()
        if row is not None:
            return row
    return None


# ---------------------------------------------------------------- 对外

def lookup(raw_word: str) -> dict[str, Any] | None:
    """查一个词。返回 `None` = 未收录。永不抛异常（词典库缺失也只是 None）。

        {word, lemma, phonetic, pos, meaning_cn, meaning_en, worth, tags, source}

    `source` 固定是 `'ecdict'`；「库文件不存在」由调用方用 `available()` 区分，
    因为那是「没装数据」而不是「没这个词」，两者的提示文案完全不同。
    """
    word = normalize_word(raw_word)
    if not word:
        return None

    conn = _connect()
    if conn is None:
        return None
    try:
        row = _fetch(conn, word)
    except sqlite3.Error as e:  # noqa: BLE001 —— 读不动就是查不到，不能让划词崩
        log.warning("ECDICT 查询失败 %r: %s", word, e)
        return None
    finally:
        conn.close()

    if row is None:
        return None

    translation = row["translation"] or ""
    return {
        "word": row["word"],
        "lemma": lemma_of(row["exchange"], row["word"]),
        "phonetic": (row["phonetic"] or "").strip(),
        "pos": parse_pos(row["pos"], translation),
        "meaning_cn": _clip(
            translation, max_chars=MAX_MEANING_CHARS, max_lines=MAX_MEANING_LINES
        ),
        "meaning_en": _clip(row["definition"], max_chars=MAX_DEFINITION_CHARS),
        "worth": _worth(
            row["tag"], row["collins"], row["oxford"], row["frq"]
        ),
        "tags": (row["tag"] or "").strip(),
        "source": "ecdict",
    }


__all__ = [
    "available",
    "lemma_of",
    "lookup",
    "normalize_word",
    "parse_pos",
    "status",
    "stripword",
]
