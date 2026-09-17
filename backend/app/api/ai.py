"""AI 接口：选段【解析】。

现在只有一种任务。**划词查词已经不在这个文件里** —— M10 起音标与释义都来自
本地词典库（`api/dictionary.py`），不再经过模型。历史类型（dict / translate /
grammar）的旧记录仍然躺在 ai_logs 里可回看，但不会再产生新的。

历史上这里还有 `/translate` 与 `/grammar` 两个端点（M3 起并存），
M8 把三个操作合并成「解析」后**刻意删掉**：留着它们等于承认「解析」不是
唯一的入口，前端迟早会有人再点回旧路；旧记录仍然躺在 ai_logs 里可回看。

`POST /api/ai/stream`（M5）是同一批任务的 SSE 版：它**不重新实现任何编排**，
只是在 `ai.run` 上挂两个回调，把「模型正在吐字」这件事透出去。落库、缓存、
入册一律仍走原路径。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..schemas import AiLogList, AiResultOut, AiStreamIn, SegmentIn
from ..services import ai, ingest, llm

log = logging.getLogger("english.api.ai")

router = APIRouter(prefix="/api/ai", tags=["ai"])

# 心跳间隔。explain 冷启动能跑 10~20 秒，中间任何一层（代理、杀软、浏览器）
# 看到长时间没有字节流过都可能把连接掐掉。发一个 SSE 注释帧成本极低。
_HEARTBEAT_S = 12.0


def _auto_context(article_id: int, block_id: int, window: int = 2) -> str:
    """取该块前后各 window 个文本块的原文，拼成上下文。

    前端只知道自己选了哪段文字，不一定知道它在文章里的位置；
    由后端按 order_index 取邻居，比让前端拼字符串更可靠。
    """
    blocks = [b for b in ingest.get_blocks(article_id) if b["type"] != "image"]
    idx = next((i for i, b in enumerate(blocks) if b["id"] == block_id), None)
    if idx is None:
        return ""
    parts = [
        (b["text"] or "").strip()
        for b in blocks[max(0, idx - window):idx + window + 1]
        if b["id"] != block_id and (b["text"] or "").strip()
    ]
    return "\n".join(parts)[:4000]


def _guard(article_id: int, highlight_id: int | None = None) -> None:
    if ingest.get_article(article_id) is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    if highlight_id is not None:
        from ..services import highlights  # 避免循环导入

        if highlights.get_highlight(highlight_id) is None:
            raise HTTPException(status_code=404, detail="痕迹不存在")


def _run(kind: str, **kwargs) -> AiResultOut:
    try:
        data = ai.run(kind, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except llm.LLMError as e:
        # 上游不可用：502 而不是 500，前端据此提示「AI 暂时不可用，可稍后重试」
        raise HTTPException(status_code=502, detail=f"AI 调用失败：{e}") from e
    return AiResultOut(**data)


def _segment_ctx(article_id: int, block_id: int | None, context: str | None) -> str | None:
    """选段任务的上下文：前端给了就用它，没给但知道是哪一块就由后端取邻居。

    一次性端点与流式端点共用一个实现 —— 曾经是复制粘贴的多份，
    改动要改好几处，很容易漏掉一处让某类任务拿不到上下文。
    """
    return context or (_auto_context(article_id, block_id) if block_id else None)


@router.post("/explain", response_model=AiResultOut)
def explain(payload: SegmentIn) -> AiResultOut:
    """【解析】：译文 + 主干/附加信息拆解 + 可复用语法要点（含自动入清单）。

    路径名保留 `explain` 而不是改成 `parse`：ai_logs 里已有的历史记录
    用的就是 `prompt_type='explain'`，改名会让新旧记录失去同一个检索键。
    用户看到的叫「解析」，内部叫什么都行。
    """
    _guard(payload.article_id, payload.highlight_id)
    return _run(
        "explain",
        article_id=payload.article_id,
        text=payload.text,
        context=_segment_ctx(payload.article_id, payload.block_id, payload.context),
        highlight_id=payload.highlight_id,
        force=payload.force,
    )


# ---------------------------------------------------------------- 流式（M5）

def _frame(event: str, data: Any) -> str:
    """拼一个 SSE 帧。

    `ensure_ascii=False` + json.dumps 是关键：模型输出里必然有换行，
    而 SSE 的帧分隔就是空行 —— 直接塞原文会把一个帧劈成好几个，
    前端收到的 JSON 全是半截。json.dumps 会把换行转义成 `\\n`，
    于是「任何字符串都装得进一行 data:」这件事由序列化层保证，不靠内容自觉。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_SENTINEL = object()


@router.post("/stream")
def stream(payload: AiStreamIn) -> StreamingResponse:
    """流式版的一次 AI 调用。

    事件（前端按 `event:` 名分发）：
      open   连接已建立，附 prompt_version（前端可据此判断缓存是否有效）
      delta  正文增量。`text` 为空串表示「上游在思考，还没有正文」——当心跳用
      reset  上游重试了，请清空已显示的增量（否则两次输出会首尾相接）
      done   完整结果，结构与 `POST /api/ai/<kind>` 的响应逐字相同
      error  `{status, detail}`，与一次性端点的状态码语义一致

    两处刻意的做法：

    1. **AI 调用跑在独立线程里**，事件经 `queue.Queue` 交给 async generator。
       `ai.run` 是同步的（SQLite 短连接 + httpx 同步客户端），直接在
       async generator 里 await 它会阻塞事件循环 —— 一个用户等 explain
       的十几秒里，整个服务都发不出任何响应。线程 + 队列是这里最小代价的解。
    2. **客户端断开不取消 AI 调用**。线程会照常跑完、照常落库 ——
       需求 5 要的是「AI 协助过的内容全部记录」，用户在等待时手滑关掉面板
       不该让这次调用白花钱且不留痕迹。
    """
    kind = payload.kind
    if kind not in ai.PROMPTS:
        raise HTTPException(status_code=422, detail=f"未知的任务类型：{kind}")
    _guard(payload.article_id, payload.highlight_id)

    text = payload.text
    ctx = _segment_ctx(payload.article_id, payload.block_id, payload.context)

    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            data = ai.run(
                kind,
                article_id=payload.article_id,
                text=text,
                context=ctx,
                highlight_id=payload.highlight_id,
                force=payload.force,
                on_delta=lambda piece: events.put({"type": "delta", "text": piece}),
                on_retry=lambda: events.put({"type": "reset"}),
            )
            events.put({"type": "done", "data": data})
        except ValueError as e:
            events.put({"type": "error", "status": 422, "detail": str(e)})
        except llm.LLMError as e:
            events.put({"type": "error", "status": 502, "detail": f"AI 调用失败：{e}"})
        except Exception as e:  # noqa: BLE001 —— 流已经开始，只能用 error 帧收尾
            log.exception("流式 AI 调用异常")
            events.put(
                {"type": "error", "status": 500, "detail": f"{type(e).__name__}: {e}"[:300]}
            )
        finally:
            events.put(_SENTINEL)

    threading.Thread(target=work, name=f"ai-stream-{kind}", daemon=True).start()

    async def frames():
        yield _frame("open", {"kind": kind, "prompt_version": ai.PROMPT_VERSION})
        loop = asyncio.get_running_loop()
        while True:
            try:
                item = await loop.run_in_executor(None, events.get, True, _HEARTBEAT_S)
            except queue.Empty:
                yield ": ping\n\n"      # SSE 注释帧：不触发任何事件，只为了让字节流动
                continue
            if item is _SENTINEL:
                break
            typ = item["type"]
            if typ == "delta":
                yield _frame("delta", {"text": item["text"]})
            elif typ == "reset":
                yield _frame("reset", {"reason": "上游重试，请清空增量"})
            elif typ == "done":
                yield _frame("done", item["data"])
            else:
                yield _frame("error", {"status": item["status"], "detail": item["detail"]})

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 有反代在前面时禁掉缓冲，否则流会被攒成一大块再吐，等于没流
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stats")
def stats() -> dict:
    """调用量 / 缓存命中 / token 消耗。调 prompt 或排查缓存问题时看这个。"""
    return ai.stats()


@router.get("/health")
def health() -> dict:
    """探一次真实请求，确认 key 与模型名都可用。"""
    return {
        "model": llm.model_name(),
        "prompt_version": ai.PROMPT_VERSION,
        "ping": llm.ping(),
    }


@router.get("/logs", response_model=AiLogList)
def all_logs(
    article_id: int | None = Query(None),
    highlight_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> AiLogList:
    data = ai.list_logs(
        article_id=article_id, highlight_id=highlight_id, limit=limit, offset=offset
    )
    return AiLogList(total=data["total"], items=data["items"])


@router.delete("/logs/{log_id}")
def delete_log(log_id: int, with_highlight: bool = Query(False)) -> dict:
    """删除一条解析记录。

    「划词即翻译」意味着误操作天生无法避免（手一抖划中一个词就会调一次、
    落一条记录），这条路径给它一个清理口。删的是**这一次 AI 调用**：
    生词本条目不受影响 —— 想撤销的是那次查询，不是那个词。

    `with_highlight=1`（阅读页面板用）时，若这处划线不再被任何其他记录引用，
    连它一并删掉：记录删光后它只是一处没有解释的孤痕（还占着清单的统计）。
    同一高亮还挂着别的记录时不删 —— 那些记录仍要靠它指回原文。
    默认 0 保持旧语义：检索页只清记录，不动原文里的标注。

    缓存随记录一起消失：同一段文字再被划中会重新调模型、重新落一条流水。
    """
    result = ai.delete_log(log_id, with_highlight=with_highlight)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True, "highlight_deleted": result["highlight_deleted"]}
