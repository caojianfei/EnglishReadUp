"""后台抓取任务。

为什么必须异步（docs/DESIGN.md §5.1）：
Sixth Tone 这类客户端渲染站点要走 Playwright，实测 41 秒/篇。
同步接口必然超时，所以提交即返回 article_id，前端轮询 status。
线程池而不是 asyncio：Playwright 的 sync API 只能在非事件循环线程里跑。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from ..config import settings
from . import fetcher, ingest

log = logging.getLogger("english.jobs")

_executor = ThreadPoolExecutor(max_workers=settings.job_workers, thread_name_prefix="fetch")
_inflight: set[int] = set()
_lock = threading.Lock()


def is_running(article_id: int) -> bool:
    with _lock:
        return article_id in _inflight


def shutdown(wait: bool = False) -> None:
    _executor.shutdown(wait=wait)


def in_flight() -> set[int]:
    with _lock:
        return set(_inflight)


def submit_fetch(article_id: int, force_render: bool = False) -> bool:
    """把抓取任务丢进线程池。已在跑则返回 False（防重复入队）。"""
    with _lock:
        if article_id in _inflight:
            return False
        _inflight.add(article_id)
    _executor.submit(_run, article_id, force_render)
    return True


def _run(article_id: int, force_render: bool) -> None:
    try:
        art = ingest.get_article(article_id)
        if art is None:
            log.warning("article %s 不存在，跳过抓取", article_id)
            return

        ingest.mark_status(article_id, "fetching")
        log.info("开始抓取 #%s %s", article_id, art["url"])

        def emit(msg: str) -> None:
            log.info("[#%s] %s", article_id, msg)

        res = fetcher.fetch_article(
            art["url"], asset_key=str(article_id), force_render=force_render, log=emit
        )

        if res.ok:
            ingest.save_fetch(article_id, res)
            log.info(
                "抓取完成 #%s: blocks=%s images=%s/%s words=%s %.1fs",
                article_id, res.report.blocks_total, res.report.images_ok,
                res.report.images_ok + res.report.images_failed, res.report.words,
                res.report.elapsed_s,
            )
        else:
            err = "; ".join(res.report.errors) or f"status={res.report.status}"
            ingest.mark_status(article_id, "failed", err)
            log.warning("抓取失败 #%s: %s", article_id, err)
    except Exception as exc:  # noqa: BLE001
        log.exception("抓取任务异常 #%s", article_id)
        ingest.mark_status(article_id, "failed", f"{type(exc).__name__}: {exc}")
    finally:
        with _lock:
            _inflight.discard(article_id)
