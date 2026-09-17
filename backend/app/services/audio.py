"""发音音频：有道优先 + 词典录音回退 + 一律本地化 + **点击时才下载**。

需求（2026-09-15 起）：发音用有道 dictvoice；有道没有该词 → 回退到词典录音
（Wiktionary / Wikimedia Commons）；一律下载到本地，播放不再走外网。

与音标的分工：
  音标只认本地词典库（ECDICT + ipa-dict，红线 3）。这里只管**录音**，
  两者互不设卡 —— 有道的录音里有这个词，不代表词典收了它的词条，反之亦然。

## 英美双槽（M10）

`audio_cache` 的主键是 `(word, accent)`：**一个词 × 一个口音 = 一行**。
历史上是一词一行、只存一个口音，双口音需求进来后如果靠加列解决，会有
`path_uk/status_uk/origin_uk/path_us/...` 这样十来个新列 —— 复合主键既不用
把列翻倍，也正好对应「一个可下载对象」这个现实。

## 不做预热，点喇叭才下载

早期做法是「划词时排后台任务预热」，现在去掉了：一个词要备两份音频，
预热等于把闸门流量直接翻倍，而有道是非官方接口、最怕流量形状不像人。
改成点哪个喇叭下哪个 —— 后台一份都不下，代价只是首次点击等几秒。

所以查词路径是**纯只读**的 `slots()`，下载只发生在 `ensure()`（播放端点）。

## 为什么下载要绕这么多弯

  * **严格串行**：全局一把闸门，同一时刻只有一个外向请求。并发是风控的第一特征。
  * **间隔 + 抖动**：默认 3 秒起步、再叠 0~1.5 秒随机。固定节拍（每 2.000 秒一次）
    本身就是机器特征，所以必须抖。
  * **退避重试**：失败 2s → 4s → 8s 递增，不硬刚。
  * **熔断**：连续失败 5 次就静默 10 分钟，期间一律走回退源。
    被风控盯上时最忌讳的就是「继续重试证明自己是脚本」。
  * **负缓存**：有道的 500 = 这个词确实没有录音，这是**确定的否定**，
    落 `status='missing'` 永不重试。不缓存的话每次点播放都白打一发。
  * **只下用户真正点过的词**：不做全量预热，不扫生词本批量补。

本地化：文件按**内容 sha256** 命名（`data/audio/<16位>.mp3`），
同一个词换源重下、或两个词共用一份录音，都不会产生重复文件。
播放统一走 `GET /api/dict/{word}/audio?accent=uk|us`，浏览器永远只连本机。
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings
from ..db import now_iso, read, tx

log = logging.getLogger("english.audio")

STATUS_OK = "ok"
STATUS_MISSING = "missing"     # 确认没有录音（不是失败，别重试）
STATUS_PENDING = "pending"

# 判定：ok 拿到 / none 明确的否定 / fail 这次没成（可重试）
OK, NONE, FAIL = "ok", "none", "fail"

# 支持的口音。顺序就是词卡上两个喇叭的显示顺序（英 → 美，与音标两栏一致）。
ACCENTS = ("uk", "us")

# 有道 dictvoice 的 type 参数：1 英音、2 美音。需求是「英美都要」，
# 所以这个映射是双口音的落脚点，改它等于改所有发音的口音。
YOUDAO_TYPE = {"uk": 1, "us": 2}

# 音频容器的魔数。有道给 mp3（带 ID3 头），Commons 那边是 ogg/wav。
MAGIC_AUDIO: tuple[tuple[bytes, str], ...] = (
    (b"ID3", ".mp3"),
    (b"OggS", ".ogg"),
    (b"RIFF", ".wav"),
    (b"fLaC", ".flac"),
    (b"\x1aE\xdf\xa3", ".webm"),
)


def sniff_audio(data: bytes) -> str | None:
    """从字节魔数判断是不是音频、是哪种。判不出返回 None。

    为什么不能只信 `Content-Type`：有道出错时可能回 HTTP 200 + HTML 错误页，
    光看响应头会把它当成一段合法音频存下来，然后在浏览器里静默不出声。
    """
    for magic, ext in MAGIC_AUDIO:
        if data.startswith(magic):
            return ext
    # mp3 裸流没有 ID3 头，靠帧同步字（连续 11 个 1）认
    if len(data) > 3 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    return None


def norm_accent(accent: str | None) -> str:
    """把外部传入的口音收进白名单，认不出来就用配置里的默认口音。"""
    a = (accent or "").strip().lower()
    return a if a in ACCENTS else settings.audio_accent


# ---------------------------------------------------------------- 闸门
class _Gate:
    """全局串行闸门：最小间隔 + 抖动 + 失败计数 + 熔断。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0
        self._streak = 0
        self._cooldown_until = 0.0

    def cooldown_left(self) -> float:
        left = self._cooldown_until - time.monotonic()
        return left if left > 0 else 0.0

    def acquire(self) -> None:
        """占一次「发言权」。持锁睡眠 —— 正是靠这个把并发压成 1。"""
        with self._lock:
            gap = settings.audio_min_interval + random.uniform(0.0, settings.audio_jitter)
            wait = self._last + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def ok(self) -> None:
        with self._lock:
            self._streak = 0

    def fail(self) -> None:
        with self._lock:
            self._streak += 1
            if self._streak >= settings.audio_breaker_after:
                self._cooldown_until = time.monotonic() + settings.audio_breaker_cooldown
                log.warning(
                    "有道连续失败 %d 次，熔断 %.0f 秒（期间只用回退源）",
                    self._streak,
                    settings.audio_breaker_cooldown,
                )
                self._streak = 0


gate = _Gate()


def breaker_open() -> bool:
    """熔断中？给验收脚本与 /api/health 用。"""
    return gate.cooldown_left() > 0


# ---------------------------------------------------------------- 取远端
def youdao_url(word: str, accent: str) -> str:
    """有道发音地址。`type=1` 英音、`type=2` 美音。"""
    t = YOUDAO_TYPE.get(norm_accent(accent), 1)
    return f"{settings.dict_audio_base}?audio={quote(word, safe='')}&type={t}"


def _youdao_headers() -> dict[str, str]:
    """伪装成浏览器点读音量按钮时发的那一发请求，别留脚本痕迹。"""
    return {
        "User-Agent": settings.audio_ua,
        "Referer": "https://dict.youdao.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }


def fetch_youdao(word: str, accent: str) -> tuple[bytes | None, str, str]:
    """拉一次有道的发音，返回 `(字节 | None, ok|none|fail, 说明/扩展名)`。

    关键的一处判断：有道用 **HTTP 500** 表示「查不到 / 没有这条音」。
    这不是服务故障 —— 它是「否定」。所以它既不计失败、也不重试，
    直接换回退源。分辨错的话，一个不存在的词会把熔断器推下去。
    """
    left = gate.cooldown_left()
    if left > 0:
        return None, FAIL, f"breaker({left:.0f}s)"

    url = youdao_url(word, accent)
    note = "no-attempt"
    for attempt in range(max(1, settings.audio_max_attempts)):
        gate.acquire()
        try:
            r = httpx.get(
                url,
                headers=_youdao_headers(),
                timeout=settings.audio_timeout,
                follow_redirects=True,
            )
        except Exception as e:  # noqa: BLE001
            note = f"exc:{type(e).__name__}"
            gate.fail()
        else:
            body = r.content
            if r.status_code in (404, 500):
                gate.ok()
                return None, NONE, f"http{r.status_code}"
            if r.status_code == 200 and body:
                ext = sniff_audio(body)
                if ext and len(body) >= settings.audio_min_bytes:
                    gate.ok()
                    return body, OK, ext
                note = f"not-audio({(r.headers.get('content-type') or '?')},{len(body)}B)"
                gate.fail()
            else:
                note = f"http{r.status_code}"
                gate.fail()

        if attempt + 1 < max(1, settings.audio_max_attempts):
            time.sleep(settings.audio_backoff_base * (2**attempt))
    return None, FAIL, note


def fetch_commons(url: str) -> tuple[bytes | None, str, str]:
    """回退源：Wikimedia Commons。不是敏感接口，但照样排在同一条闸门后面。"""
    gate.acquire()
    try:
        r = httpx.get(
            url,
            headers={
                "User-Agent": settings.audio_ua,
                "Referer": "https://en.wiktionary.org/",
                "Accept": "audio/*,*/*;q=0.8",
            },
            timeout=settings.audio_timeout,
            follow_redirects=True,
        )
    except Exception as e:  # noqa: BLE001
        return None, FAIL, f"exc:{type(e).__name__}"

    if r.status_code in (404, 410):
        return None, NONE, f"http{r.status_code}"
    if r.status_code != 200 or not r.content:
        return None, FAIL, f"http{r.status_code}"
    ext = sniff_audio(r.content)
    if not ext:
        return None, NONE, "not-audio"
    return r.content, OK, ext


def _fallback_candidates(word: str, accent: str) -> list[tuple[str, str, str]]:
    """词典录音候选（Wiktionary），**惰性**获取：只有有道拿不到时才来这一趟。

    以前候选是查词时顺带解析好存进 `fallback_json` 的；不做预热之后那列没了，
    改成失败路径上现场去抓一次 —— 抓取同样走 `gate`，所以不会绕过节流。
    延迟导入：`dictionary` 顶层 import 了本模块，模块级再 import 会成环。
    """
    from . import dictionary

    out: list[tuple[str, str, str]] = []
    for c in dictionary.audio_fallback(word, accent):
        url = str(c.get("url") or "")
        if url:
            out.append(("wiktionary", url, str(c.get("accent") or "other")))
    return out


# ---------------------------------------------------------------- 落盘
def _store(data: bytes, ext: str) -> tuple[str, str]:
    """写入 data/audio/，返回 (文件名, sha256)。按内容命名，天然去重。"""
    h = hashlib.sha256(data).hexdigest()
    name = h[:16] + ext
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    path = settings.audio_dir / name
    if not path.is_file():
        path.write_bytes(data)
    return name, h


def _abs_path(row: Any) -> Path | None:
    if not row or not row["path"]:
        return None
    return settings.audio_dir / str(row["path"])


def _row(word: str, accent: str) -> sqlite3.Row | None:
    with read() as conn:
        return conn.execute(
            "SELECT * FROM audio_cache WHERE word = ? AND accent = ?",
            (word, accent),
        ).fetchone()


def _write(word: str, accent: str, **fields: Any) -> None:
    """UPSERT 一行（词 × 口音）。没登记过也能直接写（端点被单独调用的情况）。"""
    cols = ["word", "accent", *fields.keys()]
    vals: list[Any] = [word, accent, *fields.values()]
    sets = ", ".join(f"{k} = excluded.{k}" for k in fields)
    with tx() as conn:
        conn.execute(
            f"INSERT INTO audio_cache ({', '.join(cols)})"
            f" VALUES ({', '.join('?' * len(cols))})"
            f" ON CONFLICT(word, accent) DO UPDATE SET {sets}",
            vals,
        )


# ---------------------------------------------------------------- 下载
def download(word: str, accent: str | None = None, *, force: bool = False) -> dict[str, Any]:
    """把某个口音的发音抓下来落盘。**同步**，由播放端点调用（不预热、不排任务）。"""
    accent = norm_accent(accent)
    row = _row(word, accent)
    if row and not force:
        if row["status"] == STATUS_OK:
            p = _abs_path(row)
            if p and p.is_file():
                return {"status": STATUS_OK, "origin": row["origin"],
                        "accent": row["got_accent"] or accent, "hit": True}
        elif row["status"] == STATUS_MISSING:
            return {"status": STATUS_MISSING, "origin": row["origin"],
                    "accent": row["got_accent"] or accent, "hit": True}

    attempts = int(row["attempts"] or 0) if row else 0

    verdicts: list[str] = []
    note = "no-source"

    # ---- 1) 有道（主源）----
    if settings.audio_enabled:
        data, verdict, detail = fetch_youdao(word, accent)
        verdicts.append(verdict)
        note = f"youdao:{detail}"
        if verdict == OK and data:
            return _commit(word, accent, data, detail, "youdao", accent, attempts, note)

    # ---- 2) 词典录音（惰性回退）----
    for origin, url, got_accent in _fallback_candidates(word, accent):
        data, verdict, detail = fetch_commons(url)
        verdicts.append(verdict)
        note = f"{origin}:{detail}"
        if verdict == OK and data:
            return _commit(word, accent, data, detail, origin, got_accent, attempts, note)

    # 全都没拿到。必须区分两种性质完全不同的结果：
    #   * 所有候选都**明确否定** → 这词真的没有录音，记 missing 永久负缓存；
    #   * 只要有一次是 fail（超时、熔断、HTML 错误页）→ 留 pending，以后还能试。
    # 混为一谈的话，一次网络抖动就会让这个词永远哑掉。
    status = (
        STATUS_MISSING
        if verdicts and all(v == NONE for v in verdicts)
        else STATUS_PENDING
    )
    _write(
        word, accent,
        status=status,
        origin=None,
        got_accent=None,
        path=None,
        bytes=None,
        sha256=None,
        attempts=attempts + 1,
        note=note[:200],
        fetched_at=now_iso(),
    )
    log.info("发音未取到 %s[%s]：%s（记为 %s）", word, accent, note, status)
    return {"status": status, "origin": None, "accent": accent, "hit": False}


def _commit(
    word: str, accent: str, data: bytes, ext: str, origin: str,
    got_accent: str, attempts: int, note: str,
) -> dict[str, Any]:
    name, sha = _store(data, ext)
    _write(
        word, accent,
        status=STATUS_OK,
        origin=origin,
        got_accent=got_accent,
        path=name,
        bytes=len(data),
        sha256=sha,
        attempts=attempts + 1,
        note=f"ok:{origin}",
        fetched_at=now_iso(),
    )
    log.info("发音已本地化 %s[%s]（%s/%s, %d B）", word, accent, origin, got_accent, len(data))
    return {"status": STATUS_OK, "origin": origin, "accent": got_accent, "hit": False}


# ---------------------------------------------------------------- 对外
def slot(word: str, row: sqlite3.Row | None, accent: str) -> dict[str, Any]:
    """把一行台账变成前端要的槽位描述。**不碰网络、不写库。**

    `accent` 是**请求的槽位**（也是主键的一半）；`origin` 与返回值里的 `accent`
    描述的是**实际拿到的**录音 —— 有道拿不到而回退到词典录音时，可能拿到另一个
    口音，前端照实显示才不会把英音标成美音。

    公开给生词本用：`vocab` 批量装配一页卡片时也要拼同样的槽位结构，
    两处各写一份迟早会漂移（比如 URL 少一个参数）。
    """
    local = _abs_path(row)
    ready = bool(local and local.is_file())
    status = (row["status"] if row else STATUS_PENDING) or STATUS_PENDING
    # 台账说 ok 但文件不在了（data/audio 被手工清过）→ 降级成 pending，
    # 让点播放时重新下，而不是吐一个必然 404 的地址出去。
    if status == STATUS_OK and not ready:
        status = STATUS_PENDING
    return {
        "status": status,
        "origin": row["origin"] if row else None,
        "accent": (row["got_accent"] if row else None) or accent,
        "ready": ready,
        "url": None if status == STATUS_MISSING else (
            f"/api/dict/{quote(word, safe='')}/audio?accent={accent}"
        ),
    }


def slots(word: str) -> dict[str, dict[str, Any]]:
    """**只读**：两个口音槽的当前状态，没有行就是 pending。

    这是查词路径唯一的发音调用 —— 不写库、不下载、不排任务。
    顺手加回预热就等于把闸门流量摊回划词时刻了，别这么干。
    """
    return {accent: slot(word, _row(word, accent), accent) for accent in ACCENTS}


def ensure(word: str, accent: str | None = None) -> tuple[Path | None, dict[str, Any]]:
    """播放端点用：**阻塞**地保证本地有文件。没有就当场下载。"""
    accent = norm_accent(accent)
    row = _row(word, accent)
    if row is not None:
        if row["status"] == STATUS_OK:
            p = _abs_path(row)
            if p and p.is_file():
                return p, slot(word, row, accent)
        elif row["status"] == STATUS_MISSING:
            return None, slot(word, row, accent)

    if settings.audio_enabled:
        download(word, accent)
    row = _row(word, accent)
    p = _abs_path(row) if row else None
    if p and p.is_file():
        return p, slot(word, row, accent)
    return None, slot(word, row, accent)


def stats() -> dict[str, Any]:
    with read() as conn:
        rows = conn.execute(
            "SELECT status, accent, COUNT(*) c, SUM(COALESCE(bytes, 0)) b"
            " FROM audio_cache GROUP BY status, accent"
        ).fetchall()
    by_status: dict[str, int] = {}
    by_accent: dict[str, int] = {}
    total_bytes = 0
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + int(r["c"])
        by_accent[r["accent"]] = by_accent.get(r["accent"], 0) + int(r["c"])
        total_bytes += int(r["b"] or 0)
    return {
        "cached": by_status.get(STATUS_OK, 0),
        "missing": by_status.get(STATUS_MISSING, 0),
        "pending": by_status.get(STATUS_PENDING, 0),
        "by_accent": by_accent,
        "bytes": total_bytes,
        "breaker": breaker_open(),
        "default_accent": settings.audio_accent,
    }


__all__ = [
    "ACCENTS",
    "breaker_open",
    "download",
    "ensure",
    "fetch_commons",
    "fetch_youdao",
    "norm_accent",
    "slot",
    "slots",
    "sniff_audio",
    "stats",
    "youdao_url",
]
