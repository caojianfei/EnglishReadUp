"""查词编排：英美音标 + 释义，全部来自本地词典库（红线 3）。

分工（M10 起）：

    ECDICT     释义 / 词性 / 考纲标签 / 词频（官方预编译 SQLite，340 万词条）
    ipa-dict   英美双音标（美 GA / 英 RP，现代宽式 IPA）
    Wiktionary **只剩录音回退** —— 只在有道拿不到该词发音时，才去抓一次
               `{{audio}}` 候选。音标不再从这里取（理由见下）。

## 音标为什么换到 ipa-dict

以前音标来自 Wiktionary 的 `{{IPA}}` 模板：口径正确（英美双），但要联网、
首次查询约 1 秒，且必须靠 `dict_cache` 永久缓存顶住 —— 而缓存又要处理
「解析器升级后旧行作废」这种事。换成本地库之后：查词零外网、毫秒级、
没有缓存与失效这回事。

质量上也不亏：ipa-dict 是给学习者用的现代 IPA，实测对**常见词**覆盖
88~96%（柯林斯星级词 英 88% / 美 92%；牛津核心词 英 90% / 美 96%）。
ECDICT 自带的音标留作**兜底**：它只覆盖 10%，而且混着老式写法
（`ә` 当 `ə`、`:` 当 `ː`），所以只在前两者都取不到时才用，前端另有
一层只改渲染的写法归一化（`frontend/src/lib/phonetic.ts`）。

## 变形词

Wiktionary 时代要靠模型给 lemma 才能回查原形音标；现在 ECDICT 的 `exchange`
字段自带 `0:原形`（`preserved` → `preserve`），本地就能拿到，所以
「划到变形词 → 音标自动取自原形」这条能力原样保留，且不再依赖模型。

## 词卡按词形，生词本按原形

同一个词条有两个消费者，口径**故意不一样**：

    词卡（`lookup`，即 `/api/dict/{word}`）
        词形优先 —— 划 `Experts` 给你 `Experts` 的读音 `/ˈɛkspɜːts/`：
        人在文章里读到的正是这个形式，卡片上词头也就是它。
    生词本（`lemma_entry`）
        原形同源 —— 那一行是按 `lemma` 建的（`vocab.lemma` 唯一），词头
        就是 `expert`，音标与释义必须一起是 `expert` 的。否则会出现
        「expert /ˈɛkspɜːts/、释义写「（expert的复数形式）」」这种自相矛盾的
        行（M11 实测：69 条里 25 条是这么来的）。

所以入册前要过一次 `lemma_entry`：只换数据来源，不改查词口径。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings
from . import audio, dictdata, ecdict, ipa
from .ecdict import normalize_word

log = logging.getLogger("english.dict")

WIKTIONARY_API = settings.dict_api_base
UA = "EnglishReadUp/0.1 (personal study tool; contact: local)"

# ---------------------------------------------------------------- 录音候选解析
#
# 下面这一小段是 Wiktionary 时代留下的解析器里**唯一还有用**的部分：
# 从 `{{audio|en|<文件>|a=<口音>}}` 里抠录音地址。音标解析（`{{IPA}}` 那一套）
# 已经删掉了 —— 音标只认本地词典库。

# 模板里的音频文件名：位置参数里第一个长得像媒体文件的
_AUDIO_TPL = re.compile(r"\{\{\s*audio\s*\|([^{}]*)\}\}", re.I)
_AUDIO_FILE = re.compile(r"\.(?:ogg|oga|opus|wav|mp3|flac|m4a)$", re.I)
# 具名参数（`a=Southern England`）。音频这边口音**只在具名参数里**，得单独读一份。
_NAMED_PARAM = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*([^|=]*)")
_COMMONS_FILE = "https://commons.wikimedia.org/wiki/Special:FilePath/"
# 重音标注：{{a|GA}} {{a|RP}} {{a|US}} 等
_ACCENT_TPL = re.compile(r"\{\{\s*a\s*\|([^{}]*)\}\}", re.I)
# 章节切分：一次抓出所有标题，用「层级」判断边界
_HEAD = re.compile(r"^(={2,4})\s*([^=]+?)\s*\1\s*$", re.M)

UK_LABELS = {"rp", "uk", "british", "received pronunciation", "england"}
US_LABELS = {"ga", "genam", "us", "american", "general american"}

# Lingua Libre 的 `a=` 给的是**地名**（`a=Southern England`、`a=Texas`），
# 不是 `RP` / `GA` 这类音系标签。按子串匹配地名词表把口音归到英美；
# 表里没有的（澳大利亚、爱尔兰、印度…）落到 audio 通用槽，**不硬塞进英美**。
_UK_PLACES = (
    "england", "britain", "british", "london", "essex", "kent", "sussex",
    "yorkshire", "lancashire", "manchester", "liverpool", "birmingham",
    "bristol", "norfolk", "devon", "cornwall", "scotland", "scottish",
    "wales", "welsh", "glasgow", "edinburgh", "belfast",
)
_US_PLACES = (
    "united states", "america", "american", "texas", "california",
    "new york", "new jersey", "boston", "chicago", "seattle", "oregon",
    "florida", "midwest", "midwestern", "pacific northwest", "ohio",
    "pennsylvania", "virginia", "carolina", "georgia", "michigan",
    "minnesota", "wisconsin", "colorado", "arizona", "washington",
)


def primary_phonetic(uk: str | None, us: str | None) -> str | None:
    """展示用的**单条**音标：美音优先。

    全库只有这一处口径 —— `vocab._row` / `summarize` / `search` 都调它。
    词卡会把英美两条都列出来，但生词本、汇总清单、检索结果都是一行一条，
    得有一个统一的取舍，否则同一个词在四个页面上会显示成两三种样子。
    """
    return us or uk


# ---------------------------------------------------------------- Wiktionary 片段

def _slice_section(wikitext: str, heading: str, level: int = 2) -> str:
    """取出某个标题下的正文。

    边界是「下一个层级 <= 本标题的标题」，所以三级标题
    `===Pronunciation===` 不会被同级或更高级的标题提前截断。
    """
    heads = [
        (m.start(), m.end(), len(m.group(1)), m.group(2).strip())
        for m in _HEAD.finditer(wikitext)
    ]
    for i, (_, end, lv, title) in enumerate(heads):
        if lv != level or title.lower() != heading.lower():
            continue
        stop = len(wikitext)
        for start_n, _, lv_n, _ in heads[i + 1:]:
            if lv_n <= lv:
                stop = start_n
                break
        return wikitext[end:stop]
    return ""


def _params(body: str) -> list[str]:
    """拆模板参数，丢掉 named（含 `=`）的那些。"""
    return [p.strip() for p in body.split("|") if "=" not in p]


def _named_params(body: str) -> dict[str, str]:
    """拆模板的具名参数：`{{audio|en|f.wav|a=Southern England}}` → `{'a': 'Southern England'}`。"""
    return {k.strip().lower(): v.strip() for k, v in _NAMED_PARAM.findall(body)}


def _accent_of_label(label: str) -> str | None:
    """把 `a=Texas` 这类地名标签归成 uk / us。表里没有的返回 None。"""
    low = label.strip().lower()
    if not low:
        return None
    if low in UK_LABELS:
        return "uk"
    if low in US_LABELS:
        return "us"
    if any(p in low for p in _UK_PLACES):
        return "uk"
    if any(p in low for p in _US_PLACES):
        return "us"
    return None


def _audio_accent(body: str, fname: str) -> str | None:
    """这段录音是英音、美音，还是分不出来（None）。"""
    named = _named_params(body)
    for key in ("a", "accent", "pron", "lang"):
        if (label := named.get(key)) and (hit := _accent_of_label(label)):
            return hit
    low = fname.lower()
    if "en-us" in low or "us-" in low:
        return "us"
    if "en-uk" in low or "en-gb" in low or "gb-" in low or "uk-" in low:
        return "uk"
    return None


def _audio_slots(text: str) -> tuple[str | None, str | None, str | None]:
    """抠出 `{{audio|en|<文件>|a=<口音>}}`，归到英音 / 美音 / 通用三槽。

    为什么要第三槽：Wiktionary 的发音文件有两代命名。
      - 老的 Commons 文件叫 `en-us-resilient.ogg`，口音写在**文件名**里；
      - 新的 Lingua Libre 文件叫 `LL-Q1860 (eng)-Vealhurl-documentary.wav`，
        口音写在**具名参数** `a=Southern England` 里。
    分不出口音不等于没有录音，所以第三槽专门装它，宁可不说英美，也不能把录音扔了。
    """
    uk = us = other = None
    for body in _AUDIO_TPL.findall(text):
        fname = next((p for p in _params(body)[1:] if _AUDIO_FILE.search(p)), None)
        if not fname:
            continue
        # 文件名里有空格和括号（`LL-Q1860 (eng)-…`），必须转义
        url = _COMMONS_FILE + quote(fname, safe="")
        accent = _audio_accent(body, fname)
        if accent == "uk":
            uk = uk or url
        elif accent == "us":
            us = us or url
        else:
            other = other or url
    return uk, us, other


def audio_candidates(info: dict[str, Any], prefer: str = "uk") -> list[dict[str, str]]:
    """把解析出的录音排成回退候选，**请求的口音优先**。

    顺序里刻意保留「分不出口音」那一档垫在中间：它是真实存在的录音，
    只是判不出英美；而排在最后的那一侧是明确不匹配的音 —— 有声音总比没声音强，
    但要如实标注 accent，前端照实显示。
    """
    order = ("uk", "other", "us") if prefer == "uk" else ("us", "other", "uk")
    slots = {
        "uk": info.get("audio_uk"),
        "us": info.get("audio_us"),
        "other": info.get("audio"),
    }
    return [{"url": str(slots[a]), "accent": a} for a in order if slots.get(a)]


def _spelling_candidates(word: str) -> list[str]:
    """按优先级排列要尝试的拼写。

    Wiktionary **区分大小写且不做小写重定向**：`beijing` 是空页，
    只有 `Beijing` 有条目。所以小写查空后必须再试首字母大写。
    """
    out = [word]
    cap = word[:1].upper() + word[1:]
    if cap != word:
        out.append(cap)
    return out


def _fetch_wikitext(page: str, client: httpx.Client) -> tuple[int, str]:
    r = client.get(
        WIKTIONARY_API,
        params={
            "action": "parse",
            "page": page,
            "prop": "wikitext",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        },
    )
    if r.status_code != 200:
        return r.status_code, ""
    data = r.json()
    # 词条不存在时 error.code == 'missingtitle'，wikitext 为空串
    return 200, (data.get("parse", {}) or {}).get("wikitext", "")


def audio_fallback(word: str, prefer: str = "uk", timeout: float = 20.0) -> list[dict[str, str]]:
    """抓一次 Wiktionary，返回按口音排好的录音候选。

    **只在「有道拿不到」这条失败路径上被调用**（见 `services/audio.py` 的
    `_fallback_candidates`），所以划词路径依然零外网。请求本身由调用方排在同一条
    闸门后面（`audio.gate`），不会绕过节流。

    永不抛异常：回退源拿不到是常态，返回空列表即可。
    """
    try:
        with httpx.Client(
            timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True
        ) as client:
            for page in _spelling_candidates(word):
                status, wikitext = _fetch_wikitext(page, client)
                if status != 200 or wikitext:
                    break
    except Exception as e:  # noqa: BLE001
        log.warning("录音候选抓取失败 %s: %s", word, e)
        return []

    if status != 200 or not wikitext:
        return []

    en = _slice_section(wikitext, "English") or wikitext
    pron = _slice_section(en, "Pronunciation", 3) or _slice_section(en, "Pronunciation", 2)
    uk, us, other = _audio_slots(pron or en)
    return audio_candidates({"audio_uk": uk, "audio_us": us, "audio": other}, prefer=prefer)


# ---------------------------------------------------------------- 查词

def _phonetics(word: str, entry: dict[str, Any]) -> tuple[str | None, str | None, str, str | None]:
    """定英美音标。返回 `(uk, us, 来源, 音标取自哪个原形)`。

    顺序即优先级：
      1. ipa-dict 命中该词形 → 现代 IPA，两条都有（最常见）
      2. 变形词（`preserved`）自己没音标 → 用**原形**的：先 ipa-dict，再 ECDICT
      3. ECDICT 自带音标兜底（老式写法，只有一条，按英式落到 uk）
    """
    hit = ipa.lookup(word)
    if hit.get("uk") or hit.get("us"):
        return hit.get("uk"), hit.get("us"), "ipa", None

    lemma = str(entry.get("lemma") or "")
    if lemma and lemma != word:
        base = ipa.lookup(lemma)
        if base.get("uk") or base.get("us"):
            return base.get("uk"), base.get("us"), "ipa", lemma
        base_entry = ecdict.lookup(lemma)
        if base_entry and base_entry["phonetic"]:
            return base_entry["phonetic"], None, "ecdict", lemma

    if entry.get("phonetic"):
        return str(entry["phonetic"]), None, "ecdict", None
    return None, None, "missing", None


def _blank(word: str, *, source: str, setup: dict[str, Any] | None = None) -> dict[str, Any]:
    """空结果的形状与正常结果**完全一致**，前端不必为「查不到」写第二套分支。

    `source` 有两种，提示文案完全不同，所以必须分开：
      * `missing`     —— 词典库在，但没这个词（数字、拼写错、极生僻）
      * `unavailable` —— 词典库文件压根没装。**这时会带上 `setup`**：服务启动时
        已经在后台自动下载了（services/dictdata.py），前端拿它显示「正在下载 42%」，
        而不是让用户去跑脚本 —— 那一步服务自己会做。
    """
    return {
        "word": word,
        "headword": word,
        "lemma": word,
        "pos": "",
        "phonetic": None,
        "phonetic_uk": None,
        "phonetic_us": None,
        "phonetic_source": "missing",
        "phonetic_of": None,
        "meaning_cn": "",
        "meaning_en": "",
        "worth": 1,
        "tags": "",
        "audios": {},
        "source": source,
        "setup": setup,
    }


def lookup(raw_word: str) -> dict[str, Any]:
    """查一个词的音标与释义。**纯本地、只读**，永不抛异常。

    返回：

        {word, headword, lemma, pos,
         phonetic, phonetic_uk, phonetic_us, phonetic_source, phonetic_of,
         meaning_cn, meaning_en, worth, tags, audios, source}

    `source`：`ecdict` 查到 / `missing` 未收录 / `unavailable` 词典库没装
    （后者会带 `setup`：自动补齐的进度，见 services/dictdata.py）。
    `audios` 是两个发音槽的**当前状态**（只读查询，不下载 —— 音频是点喇叭才下的）。
    """
    word = normalize_word(raw_word)
    if not word:
        return _blank("", source="missing")
    if not ecdict.available():
        # 只在真的缺文件时才取一次状态（它会做几次 st_size，不该压在每次成功查词上）。
        # 服务启动时已经在后台补了，所以这里多半是「正在下」而不是「要人去装」。
        log.warning(
            "ECDICT 库文件缺失：%s（启动自检会补；也可手工跑 scripts/fetch_dicts.py）",
            settings.ecdict_file,
        )
        return _blank(word, source="unavailable", setup=dictdata.hint())

    entry = ecdict.lookup(word)
    if entry is None:
        return _blank(word, source="missing")

    uk, us, ph_source, ph_of = _phonetics(word, entry)
    return {
        "word": word,
        # ECDICT 里的真实拼写（`beijing` 查到的是 `Beijing`），词卡上显示它更体面
        "headword": str(entry.get("word") or word),
        "lemma": str(entry.get("lemma") or word),
        "pos": str(entry.get("pos") or ""),
        "phonetic": primary_phonetic(uk, us),
        "phonetic_uk": uk,
        "phonetic_us": us,
        "phonetic_source": ph_source,
        "phonetic_of": ph_of,
        "meaning_cn": str(entry.get("meaning_cn") or ""),
        "meaning_en": str(entry.get("meaning_en") or ""),
        "worth": int(entry.get("worth") or 0),
        "tags": str(entry.get("tags") or ""),
        "audios": audio.slots(word),
        "source": "ecdict",
    }


def lemma_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """把词条换成「lemma 自己」的那一份，**给生词本用**（M11）。

    `lookup` 返回的是**词形**的词条（词形优先，见模块 docstring）：划 `Experts`
    拿到 `/ˈekspɜːts/` 与 ECDICT `experts` 条的释义。而 `vocab` 那一行是按
    `lemma` 建的，词头就是 `expert` —— 直接把词形数据挂上去，列表上就会出现
    「expert /ˈɛkspɜːts/、释义写（expert的复数形式）」这种词头与数据对不上的行。

    所以入册前换成原形那份：`word` / `headword` / `lemma` / 音标 / 释义 / 词性 /
    worth / tags 全部来自 `lookup(lemma)`。查词端点（词卡）不受影响。

    原形查不到（极少数 `exchange` 指向的词条本身缺失）就原样返回 —— 宁可这一行
    数据糙一点，也不能把用户划过的词弄丢。

    **只读**：调用方负责保证它在写事务之外执行（它内部会开只读连接，
    `audio.slots()` 还要读主库，叠进 `tx()` 就是历史上踩过的死锁）。
    """
    word = normalize_word(str(entry.get("word") or ""))
    lemma = normalize_word(str(entry.get("lemma") or ""))
    if not lemma or lemma == word:
        return entry
    base = lookup(lemma)
    return base if base.get("source") == "ecdict" else entry


__all__ = [
    "audio_candidates",
    "audio_fallback",
    "lemma_entry",
    "lookup",
    "normalize_word",
    "primary_phonetic",
]
