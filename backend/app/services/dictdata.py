"""离线词典数据的**准备**：启动自检 + 缺什么补什么（ECDICT / ipa-dict）。

划词的全部数据来源就这三个文件（红线 3）：

    ECDICT    释义 / 词性 / 考纲标签 / 词频（官方预编译 SQLite，约 811 MB）
    ipa-dict  英美双音标（两个 TSV，合计约 4.7 MB）

为什么要有这一层（而不只是留个脚本）：
    `data/` 是可再生的 —— 换机器、重装、手滑清空之后服务照样起得来，但词典这三个
    文件是**外部数据**，不会自己回来，划词就永远停在「本地词典库没有安装，去跑
    fetch_dicts.py」。那一步纯手工、还要等 200 MB 下载，很容易被忘掉。所以把
    「缺什么补什么」搬进服务：启动时查一遍，缺的补上。

为什么在**后台线程**里补、不阻塞启动：
    ECDICT 压缩包约 207 MiB，解压后 811 MB。阻塞启动意味着 uvicorn 几分钟不监听，
    前端连不上、健康检查也过不去 —— 比「暂时查不到词」糟糕得多。后台补还有个好处：
    文章、检索、生词本这些**不依赖词典**的功能立刻可用。

为什么下载与安装只有一份实现：
    `scripts/fetch_dicts.py` 仍然保留（多了 --verify 体检与 --force 重下，那两个只在
    手工排障时用），但它直接调用这里的函数。两个入口各写一套下载代码，迟早会出现
    「脚本装得对、服务装不对」这种最难查的分叉。

状态给谁看：
    `status()` 同时喂给 `/api/health` 与词卡的 `setup` 字段。查词命中「库没装」时
    前端拿它显示「正在下载 42%」，而不是让用户去跑脚本 —— 因为服务这边已经在下了。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("english.dictdata")

# ECDICT：官方 release 1.0.28 的预编译 SQLite 包（约 207 MiB，解压出单个 .db）。
# 同一 release 还有 mdx / eudic / mobi / stardict 等格式，只有 sqlite 这份是我们要的。
ECDICT_URL = (
    "https://github.com/skywind3000/ECDICT/releases/download/1.0.28/ecdict-sqlite-28.zip"
)

# ipa-dict：注意上游英式的文件名是 en_UK 而不是 en_GB，写错会 404。
# 落地时改名为 ipa_en_*.txt —— data/ 下还躺着文章、图片、音频等一堆东西，
# 带前缀一眼就能看出这是哪来的、给谁用的。
IPA_BASE = "https://raw.githubusercontent.com/open-dict-data/ipa-dict/master/data"
IPA_REMOTE = {"us": "en_US.txt", "uk": "en_UK.txt"}
IPA_LOCAL = {"us": "ipa_en_us.txt", "uk": "ipa_en_uk.txt"}

UA = "EnglishReadUp/0.1 (personal study tool; contact: local)"

# 流式下载的块大小。512 KiB 在进度回调的粒度与内存占用之间取个平衡。
CHUNK = 1024 * 512

# 「文件在」不等于「装好了」：中断留下的半截文件（下载器被强杀、拷到一半断电）
# 会被 is_file() 认成成品，然后每次划词都静默查不到东西 —— 最难查的那种坏。
# 所以每个数据集带一个**体积下限**，明显小于它的一律当没装、重下。
# 下限取得很宽松（真值：ECDICT 811 MB、两个 TSV 3.1 + 1.6 MB），只拦「一眼假」的残file。
ECDICT_MIN_BYTES = 50 * 1024 * 1024
IPA_MIN_BYTES = 128 * 1024

# 进度回调：(描述, 已下载字节, 总字节)。总字节拿不到（无 Content-Length）时是 0；
# `已下载字节 == 0` 的调用是**阶段提示**（「开始解压了」），不表示进度回退。
Progress = Callable[[str, int, int], None]


# ---------------------------------------------------------------- 数据集清单

def datasets() -> list[dict[str, Any]]:
    """三个数据集的现值清单。

    每次现算，不缓存：路径跟着 `DATA_DIR` / `ECDICT_DB` / `IPA_DICT_DIR` 走，
    而配置是可以改的（改完重启，清单必须跟着变）。
    """
    return [
        {
            "key": "ecdict",
            "kind": "zip",
            "label": "ECDICT 词库",
            "path": settings.ecdict_file,
            "url": ECDICT_URL,
            "min_bytes": ECDICT_MIN_BYTES,
        },
        {
            "key": "ipa_us",
            "kind": "file",
            "label": "美音音标表",
            "path": settings.ipa_dir / IPA_LOCAL["us"],
            "url": f"{IPA_BASE}/{IPA_REMOTE['us']}",
            "min_bytes": IPA_MIN_BYTES,
        },
        {
            "key": "ipa_uk",
            "kind": "file",
            "label": "英音音标表",
            "path": settings.ipa_dir / IPA_LOCAL["uk"],
            "url": f"{IPA_BASE}/{IPA_REMOTE['uk']}",
            "min_bytes": IPA_MIN_BYTES,
        },
    ]


def _installed(d: dict[str, Any]) -> bool:
    path: Path = d["path"]
    try:
        return path.is_file() and path.stat().st_size >= d["min_bytes"]
    except OSError:
        return False


def missing() -> list[dict[str, Any]]:
    """还没装好的数据集（顺序即安装顺序，ECDICT 在前 —— 它是主数据）。"""
    return [d for d in datasets() if not _installed(d)]


# ---------------------------------------------------------------- 状态

_lock = threading.Lock()          # 保护 _state 与 _thread
_work_lock = threading.Lock()     # 「同一时刻只有一条安装流水」的闸门
_thread: threading.Thread | None = None

_state: dict[str, Any] = {
    # idle 没跑过 | downloading 正在下 | ready 齐了 | error 下失败 | off 开关关掉了
    "state": "idle",
    "items": {},          # key -> {label, status, bytes, total, error}
    "error": None,
    "started_at": None,
    "finished_at": None,
}

# 单个数据集「正在忙」的两个阶段：downloading（在网络上）与 extracting（在解压）。
# 界面进度条认前者，文案两种都要认。
_BUSY_STATUS = ("downloading", "extracting")


def _mark_started(todo: list[dict[str, Any]]) -> None:
    with _lock:
        _state.update(
            state="downloading", error=None, started_at=time.time(), finished_at=None,
            items={
                d["key"]: {
                    "label": d["label"], "status": "pending",
                    "bytes": 0, "total": 0, "error": None,
                }
                for d in todo
            },
        )


def _item_progress(key: str, outer: Progress | None) -> Progress:
    """下载进度：一份写进状态（界面读），一份转给调用方（脚本要打印）。

    `got == 0` 的调用是**阶段提示**（见 `_install_zip` 的「解压」），不覆盖已记下的
    字节数 —— 否则界面上的百分比会从 100% 掉回 0% 再涨回去。
    """
    def cb(label: str, got: int, total: int) -> None:
        with _lock:
            item = _state["items"].get(key)
            if item is not None and got:
                item["status"] = "downloading"
                item["bytes"] = got
                if total:
                    item["total"] = total
        if outer is not None:
            outer(label, got, total)

    return cb


def _item_stage(key: str, status: str) -> None:
    """把某个数据集标成非下载中的阶段（目前只有 `extracting`）。"""
    with _lock:
        item = _state["items"].get(key)
        if item is not None:
            item["status"] = status


def _item_done(key: str, size: int) -> None:
    with _lock:
        item = _state["items"].get(key)
        if item is not None:
            item.update(status="done", bytes=size, total=item["total"] or size, error=None)


def _item_failed(key: str, error: str) -> None:
    with _lock:
        item = _state["items"].get(key)
        if item is not None:
            item.update(status="error", error=error)


def _settle(state: str, error: str | None = None) -> None:
    with _lock:
        _state.update(state=state, error=error, finished_at=time.time())


# ---------------------------------------------------------------- 下载 / 安装

def _download(
    url: str, dest: Path, *, label: str, on_progress: Progress | None = None
) -> None:
    """流式下载到 `dest`：先写 `.part` 再改名。

    「先写 .part 再改名」不只是为了不留半截文件 —— **服务运行中也在下载**（启动自检），
    而查词是无锁读同一个路径的。`.replace()` 是原子操作，读者要么看到旧状态
    （文件不存在），要么看到完整成品，绝不会读到写了一半的 SQLite。

    失败时把 `.part` 删掉：不实现断点续传，留着只会让下次运行时多一个可疑文件。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True,
            timeout=httpx.Timeout(60.0, read=600.0),
            headers={"User-Agent": UA},
        ) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(CHUNK):
                    f.write(chunk)
                    got += len(chunk)
                    if on_progress:
                        on_progress(label, got, total)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _rmtree(path: Path) -> None:
    """删目录，Windows 上需要重试几次。

    实测踩到过：解压目录里那个 811 MB 的 .db 刚被 `replace` 移走，杀毒软件还在扫它，
    这时 `shutil.rmtree` 会失败 —— 而 `ignore_errors=True` 会把失败整个吞掉，
    只在 data/ 里留一个空目录，看不出是谁留下的。所以这里自己重试并留下日志。
    """
    for attempt, delay in enumerate((0.0, 0.3, 0.8, 2.0)):
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            last = e
    log.info("解压目录没能删掉（%s），留着让下次启动的清理扫掉：%s", last, path)


def _install_zip(d: dict[str, Any], on_progress: Progress | None) -> None:
    """ECDICT：下 zip（207 MiB）→ 解压出 .db → 落到 data/。"""
    dest: Path = d["path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 解压目录刻意放在**目标同卷**（data/ 下）：解压出来 811 MB，系统临时目录未必
    # 与 data/ 在同一个分区，而 `Path.replace` 底层是 rename，跨卷会直接报错。
    tmp_dir = Path(tempfile.mkdtemp(prefix=".ecdict-", dir=dest.parent))
    try:
        zip_path = tmp_dir / "ecdict.zip"
        _download(d["url"], zip_path, label=d["label"], on_progress=on_progress)
        # 解压 811 MB 也要几十秒。不吭一声的话，命令行会像卡住了一样。
        _item_stage(d["key"], "extracting")
        if on_progress:
            on_progress(f"{d['label']}（解压中…）", 0, 0)
        with zipfile.ZipFile(zip_path) as zf:
            # 包内文件带中文名目录，不保证叫什么，只认扩展名。
            members = [n for n in zf.namelist() if n.lower().endswith(".db")]
            if not members:
                raise RuntimeError("ECDICT 压缩包里没有 .db 文件（官方包结构可能变了）")
            extracted = Path(zf.extract(members[0], tmp_dir))
        extracted.replace(dest)
    finally:
        _rmtree(tmp_dir)
    _check_ecdict(dest)


def _install_file(d: dict[str, Any], on_progress: Progress | None) -> None:
    """ipa-dict 的两个 TSV：直接落到 data/。"""
    _download(d["url"], d["path"], label=d["label"], on_progress=on_progress)
    _check_ipa(d["path"])


def _check_ecdict(path: Path) -> None:
    """装完随手确认一次：能打开、有 stardict 表、`word` 上有索引。

    为什么要这一步：200 MB 下完才发现是坏包（或换了别的构建）就白等了，
    而这里只花几毫秒 —— 都是元数据查询，不碰 340 万行。
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "stardict" not in tables:
            raise RuntimeError(f"{path.name} 里没有 stardict 表，可能不是 ECDICT 的库")
        if not ensure_word_index(conn):
            log.info("ECDICT 的 word 上没有索引，已补建（否则每次查词会退化成全表扫描）")
    finally:
        conn.close()


def _check_ipa(path: Path) -> None:
    """确认下到的是 TSV 而不是错误页 —— 上游 404 时返回的 HTML 也会被存成文件。"""
    with path.open("r", encoding="utf-8") as f:
        for _ in range(5):
            line = f.readline()
            if "\t" in line:
                return
    raise RuntimeError(f"{path.name} 不像 ipa-dict 的 TSV（前几行没有制表符）")


def ensure_word_index(conn: sqlite3.Connection) -> bool:
    """确保 `stardict.word` 上有可用索引，返回 True 表示本来就有。

    官方这份库的 `word` 声明成 NOCASE 且自带索引（`stardict_2`），这里是给
    「换了别的 ECDICT 构建」留的兜底。**必须在这里（可写连接）做**：
    运行时是 `mode=ro`，补不了 —— 而没索引意味着每次划词从 0 毫秒变成半秒
    （见 `services/ecdict.py` 的实测）。
    """
    for info in conn.execute("PRAGMA index_list('stardict')"):
        name = info[1]
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')")]
        if "word" in cols:
            return True
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stardict_word ON stardict(word)")
    conn.commit()
    return False


def _install(d: dict[str, Any], on_progress: Progress | None) -> None:
    log.info("下载 %s（%s）…", d["label"], d["url"])
    if d["kind"] == "zip":
        _install_zip(d, on_progress)
    else:
        _install_file(d, on_progress)
    log.info("%s 已安装：%s", d["label"], d["path"])


# ---------------------------------------------------------------- 对外：补齐

def ensure(force: bool = False, progress: Progress | None = None) -> dict[str, Any]:
    """同步补齐缺失的数据。`force=True` 时全部重下。返回最终 `status()`。

    单飞：整条安装流水在一把模块级锁后面，脚本与服务不会同时写同一个文件
    （用户看到启动日志里在下载、又跑去手工跑一次脚本，是很自然的事）。

    失败会往上抛（脚本要凭它给非零退出码），抛出前状态已经落成 `error` ——
    后台线程那条路径靠状态给界面看，不靠异常。
    """
    with _work_lock:
        _sweep_stale_tmp()
        todo = [d for d in datasets() if force or not _installed(d)]
        if not todo:
            _settle("ready")
            return status()

        _mark_started(todo)
        key = ""
        try:
            for d in todo:
                key = d["key"]
                _install(d, _item_progress(key, progress))
                _item_done(key, d["path"].stat().st_size)
        except Exception as e:  # noqa: BLE001 —— 网络/磁盘/包结构都可能炸，统一记状态
            log.warning("词典数据安装失败：%s", e)
            _item_failed(key, str(e))
            _settle("error", str(e))
            raise
        _settle("ready")
        return status()


def _sweep_stale_tmp(max_age_s: float = 1800.0) -> None:
    """清掉上次留下的解压目录与 `.part` 残file。

    为什么需要：ECDICT 解压期间 data/ 下会短暂存在一个 `.ecdict-xxxx/`（zip + 811 MB
    的 .db），进程被强杀、或清理时机不巧（见 `_rmtree`）就留在那里了 ——
    一个 G 的垃圾，还看不出是垃圾。

    为什么带年龄门槛而不是照单全清：脚本与服务可能同时在跑（本进程的锁管不到
    另一个进程），正在被写的那一份 mtime 一直在更新。30 分钟足够宽松 ——
    真的在下载/解压的东西不可能半小时不写一个字节。
    """
    parent = settings.data_dir
    if not parent.is_dir():
        return
    now = time.time()
    for p in [*parent.glob(".ecdict-*"), *parent.glob("*.part")]:
        try:
            if now - p.stat().st_mtime < max_age_s:
                continue
            if p.is_dir():
                _rmtree(p)
            else:
                p.unlink(missing_ok=True)
            log.info("清掉过期残留：%s", p)
        except OSError:
            pass


def ensure_async() -> None:
    """启动自检：缺什么就在后台补什么。**不阻塞启动**。

    开关关掉时只记一行日志，但仍然把状态写成 `off` —— 这样界面能说清
    「自动下载已关闭」，而不是让用户对着「词典库没有安装」猜为什么。
    """
    if not settings.dict_autodownload:
        todo = missing()
        with _lock:
            _state["state"] = "off" if todo else "ready"
        if todo:
            log.info(
                "DICT_AUTODOWNLOAD=false，跳过词典数据自检（缺 %s；要装就跑 python scripts/fetch_dicts.py）",
                "、".join(d["label"] for d in todo),
            )
        return

    todo = missing()
    if not todo:
        with _lock:
            _state["state"] = "ready"
        log.info("词典数据齐备（%s）", settings.ecdict_file)
        return

    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name="dictdata-fetch", daemon=True)
        _thread.start()
    log.warning(
        "词典数据缺失：%s —— 已在后台开始下载（ECDICT 约 207 MiB、音标表约 5 MB）。"
        "其他功能不受影响，划词要等它下完",
        "、".join(d["label"] for d in todo),
    )


def _run() -> None:
    try:
        ensure()
        log.info("词典数据补齐完成：%s", settings.ecdict_file)
    except Exception as e:  # noqa: BLE001
        # 后台线程里抛异常会静默消失（uvicorn 看不到），所以这里必须自己吞掉并说清后果。
        log.warning(
            "词典数据自动下载失败：%s。可稍后重启服务重试，或手工跑 python scripts/fetch_dicts.py",
            e,
        )


# ---------------------------------------------------------------- 对外：状态

def status() -> dict[str, Any]:
    """状态快照。`/api/health` 用它，词卡的 `setup` 是它的精简版（`hint()`）。

    这里只做 is_file / st_size 这种元数据检查，**不做 COUNT(*)、不打开 ECDICT**：
    它会被健康检查与每次「库没装」的查词调用，必须毫秒级返回。
    """
    with _lock:
        state = _state["state"]
        items = {k: dict(v) for k, v in _state["items"].items()}
        error = _state["error"]

    todo = missing()
    missing_keys = [d["key"] for d in todo]
    if missing_keys and state not in ("downloading", "error"):
        # 状态与磁盘不一致（比如把 data/ 删了、或开关被改）时的兜底口径：
        # 以磁盘为准，别报「就绪」。
        state = "off" if not settings.dict_autodownload else "idle"

    # 界面要显示的那一格：正在下的那一个 > 排在前面还没开始下的那一个。
    current = next((i for i in items.values() if i["status"] in _BUSY_STATUS), None)
    if current is None and todo:
        first = todo[0]
        current = items.get(first["key"]) or {
            "label": first["label"], "status": "pending", "bytes": 0, "total": 0, "error": None,
        }

    percent = (
        min(100, int(current["bytes"] * 100 / current["total"]))
        if current and current["status"] == "downloading" and current.get("total")
        else None
    )

    return {
        "state": state,
        "auto": settings.dict_autodownload,
        "missing": missing_keys,
        "items": items,
        # 正在下的那一个（界面拿它显示「ECDICT 词库 42%」），不在下就是 None
        "current": current,
        "percent": percent,
        "error": error,
        "text": _text(state, current, percent, error),
    }


def _brief(error: str | None, limit: int = 140) -> str:
    """把异常压成一行。

    httpx 的错误是好几行（第二行还是 MDN 链接），原样塞进词卡会很难看；
    完整的那份在 `/api/health` 与日志里，界面只需要知道「为什么没成」。
    """
    line = next((ln.strip() for ln in (error or "").splitlines() if ln.strip()), "")
    return line[: limit - 1] + "…" if len(line) >= limit else line


def _text(state: str, current: dict[str, Any] | None, percent: int | None, error: str | None) -> str:
    """一句话说明 —— 界面直接显示，所以在这里拼好，别让前端拼中文。"""
    if state == "downloading":
        if current and current["status"] == "extracting":
            return f"正在解压{current['label']}（811 MB，要几十秒）"
        if current and percent is not None:
            return (
                f"正在下载{current['label']} {percent}%"
                f"（{current['bytes'] / 1048576:.0f} / {current['total'] / 1048576:.0f} MiB）"
            )
        return f"正在下载{current['label']}…" if current else "正在下载词典数据…"
    if state == "error":
        return f"词典数据下载失败：{_brief(error)}"
    if state == "off":
        return "自动下载词典数据已关闭（DICT_AUTODOWNLOAD=false）"
    if state == "ready":
        return "词典数据已就绪。"
    return "词典数据还没装好。"


def hint() -> dict[str, Any]:
    """词卡「库没装」时给前端的精简状态：状态 + 一句话 + 当前进度。

    完整快照（每个数据集的字节数、失败原因）在 `/api/health` 里 —— 词卡上
    只需要能显示一行字，给太多字段等于逼前端做一遍同样的取舍。
    """
    s = status()
    return {
        "state": s["state"],
        "text": s["text"],
        "percent": s["percent"],
        "missing": s["missing"],
    }


__all__ = [
    "ECDICT_URL",
    "IPA_BASE",
    "IPA_LOCAL",
    "IPA_REMOTE",
    "Progress",
    "datasets",
    "ensure",
    "ensure_async",
    "ensure_word_index",
    "hint",
    "missing",
    "status",
]
