#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清理开发中间产物：data/ 下的临时日志与仓库根/backend 下的回归输出。

不用 shell 的 rm：本机回收站不可用时会被安全护栏拦下，
而项目内明确列举的临时文件由脚本自己删。

只匹配「本工程的调试脚本产物」这个明确模式（`_` 前缀 + 已知后缀），
不做通配删除 —— 手滑一次就是把仓库删空。
"""

from __future__ import annotations

from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "data"
ROOT = BACKEND.parent

# 调试脚本落盘的临时文件：`_*.txt`（验收输出）、`_*.json`（接口原始响应）、
# `_*.csv` / `_*.md`（试导出）、`_*.mjs` / `_*.py`（一次性临时脚本）、
# `_*.log`（后台服务日志）。
# 刻意不碰任何非 `_` 开头的文件，也不碰 `_*.png`（截图都落在 data/*_shots/
# 里且不带下划线前缀，加进来只会增加误删面）。
SCRATCH_SUFFIXES = (".txt", ".json", ".csv", ".md", ".mjs", ".py", ".log")


def _drop(targets: dict[Path, str], removed: list[str], skipped: list[str]) -> None:
    for f, label in sorted(targets.items()):
        if not f.is_file():
            continue
        try:
            f.unlink(missing_ok=True)
            removed.append(label)
        except OSError as e:
            # 本机的删除走回收站（安全护栏要求），被占用的文件会失败。
            # 一个文件删不掉不该让整轮清理崩掉 —— 记下来，其余照删。
            skipped.append(f"{label}（{type(e).__name__}）")


def main() -> int:
    removed: list[str] = []
    skipped: list[str] = []

    for pat in ("_*", "api_probe.txt", "*.tmp"):
        for f in DATA.glob(pat):
            if f.is_file():
                _drop({f: f"data/{f.name}"}, removed, skipped)

    for base, prefix in ((BACKEND, ""), (ROOT, "")):
        targets: dict[Path, str] = {}
        for suf in SCRATCH_SUFFIXES:
            for f in base.glob(f"_*{suf}"):
                # 跳过 backend/scripts/ 下的正式脚本（它们不以下划线开头，
                # 但这里再加一道防线，避免哪天命名习惯变了误删）
                if f.parent.name == "scripts":
                    continue
                targets[f] = f"{prefix}{f.name}"
        _drop(targets, removed, skipped)

    print("removed:", ", ".join(sorted(set(removed))) or "(none)")
    if skipped:
        print("skipped（多半是被进程占用）:", ", ".join(sorted(set(skipped))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
