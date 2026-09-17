"""ipa-dict 本地音标查询（美 GA / 英 RP）。

两个 TSV 合计 4.6 MiB，第一次查词时整份读进内存（模块级 cache + 锁），之后就是
普通字典查找、微秒级。**不建 SQLite**：这两份数据只有「词 → 音标」一个键，
没有索引需求也没有并发写，跑一次导入只是多一个需要同步的产物。

为什么音标要单独找个库：
    ECDICT 自带的音标只覆盖 10%，而且混着老式写法（用西里尔字母 `ә` 当 `ə`、
    用 `:` 当 `ː`）。ipa-dict 是给学习者用的现代宽式 IPA，实测对**常见词**
    覆盖 88~96%（柯林斯星级词：英 88% / 美 92%；牛津核心词：英 90% / 美 96%），
    所以拿它当主音标，ECDICT 那条降为兜底。命中率报告见
    `scripts/fetch_dicts.py --verify`。

数据格式：`词<TAB>/音标/`，一个词可能有多个读音（逗号分隔），只取第一个 ——
与词卡的「一条音标」口径一致，避免上面挂一长串。

许可：`en_US` 源自 cmudict-ipa + syllabify（MIT），`en_UK` 源自 ipacards
（GPL 3.0）。两个文件都不入版本库（`.gitignore` 已排除 `backend/data/*`），仅本地使用。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import settings
from .ecdict import stripword

log = logging.getLogger("english.ipa")

# 上游文件名。注意英式是 en_UK 而不是 en_GB；落地后统一改成 ipa_en_*.txt
# （data/ 下还躺着文章、图片、音频，带前缀一眼能看出这是给谁用的）。
FILES = {"us": "ipa_en_us.txt", "uk": "ipa_en_uk.txt"}

_lock = threading.Lock()
_table: dict[str, dict[str, str]] | None = None


def available() -> bool:
    return any((settings.ipa_dir / name).is_file() for name in FILES.values())


def _load() -> dict[str, dict[str, str]]:
    """读两个 TSV，返回 `{词: {accent: 音标}}`。

    只有**至少读到一个文件**才缓存：两个文件都缺失时把空表缓存下来，
    用户随后装好数据却要重启服务才能生效，是个没必要的坑。
    """
    global _table
    if _table is not None:
        return _table

    with _lock:
        if _table is not None:      # 等锁的时候别人可能已经加载完了
            return _table

        table: dict[str, dict[str, str]] = {}
        loaded = 0
        for accent, name in FILES.items():
            path = settings.ipa_dir / name
            if not path.is_file():
                log.warning("音标文件缺失：%s（%s 音标将不可用）", path, accent)
                continue
            n = 0
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 2:
                        continue
                    word, ipa = parts[0].strip(), parts[1].split(",")[0].strip()
                    if not word or not ipa:
                        continue
                    table.setdefault(word.lower(), {})[accent] = ipa
                    n += 1
            log.info("音标已加载 %s：%d 条", name, n)
            loaded += 1

        if loaded:
            _table = table
        return _table if _table is not None else {}


def lookup(word: str) -> dict[str, str]:
    """查一个词，返回 `{'uk': '/.../', 'us': '/.../'}`；缺哪边就不带哪个键。

    值是**带前后斜杠**的原样形态，与词卡既有渲染口径一致。
    """
    w = (word or "").strip().lower()
    if not w:
        return {}
    table = _load()
    hit = table.get(w)
    if hit is None:
        # 撇号/连字符的写法差异：`China's`、`long-time` 这类在 ipa-dict 里
        # 未必有对应条目，退一步按去符号形式再试一次（不额外建索引，
        # 就是一次字典查找）。
        stripped = stripword(w)
        if stripped and stripped != w:
            hit = table.get(stripped)
    return dict(hit) if hit else {}


def status() -> dict[str, Any]:
    """给 /api/health 用的轻量状态。`entries` 为 None = 还没被查过（未加载）。"""
    return {
        "files": {a: (settings.ipa_dir / n).is_file() for a, n in FILES.items()},
        "entries": len(_table) if _table is not None else None,
    }


def reset() -> None:
    """丢掉内存里的表（探针/验收脚本改数据后用，正常路径不需要）。"""
    global _table
    with _lock:
        _table = None


__all__ = ["available", "lookup", "reset", "status"]
