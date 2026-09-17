"""间隔复习接口（M5）。

队列与评分是「同一件事的两半」：队列里每张卡的四个按钮上写着「下次 1 分钟 /
10 分钟 / 1 天 / 4 天」，那是 `srs.preview()` 算的；按下去以后走 `srs.grade()`。
两者共用 `srs.schedule()`，所以按下去的结果与按钮上写的一定一致 ——
如果分开实现，这种「写着的和实际给的不一样」很难被发现。

没有 `POST /api/review/cards`（造卡）：卡片就是生词本条目，只能由划词产生。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..schemas import (
    ReviewCardOut,
    ReviewGradeIn,
    ReviewLogList,
    ReviewLogOut,
    ReviewQueueOut,
    ReviewStatsOut,
)
from ..services import srs

router = APIRouter(prefix="/api/review", tags=["review"])


@router.get("/queue", response_model=ReviewQueueOut)
def review_queue(
    limit: int = Query(srs.DEFAULT_LIMIT, ge=1, le=300, description="本轮最多几张"),
    new_limit: int = Query(
        srs.DEFAULT_NEW_LIMIT, ge=0, le=300,
        description="其中「从没复习过」的新词最多几张；到期的旧词不受它限制",
    ),
) -> ReviewQueueOut:
    return ReviewQueueOut(**srs.queue(limit=limit, new_limit=new_limit))


@router.get("/stats", response_model=ReviewStatsOut)
def review_stats() -> ReviewStatsOut:
    return ReviewStatsOut(**srs.stats())


@router.get("/logs", response_model=ReviewLogList)
def review_logs(
    vocab_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ReviewLogList:
    """复习流水。答错的所有历史都在这里，包括已经毕业又掉回来的。"""
    data = srs.list_logs(vocab_id=vocab_id, limit=limit, offset=offset)
    return ReviewLogList(total=data["total"], items=[ReviewLogOut(**r) for r in data["items"]])


def _card_or_404(vocab_id: int) -> ReviewCardOut:
    row = srs.card(vocab_id)
    if row is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return ReviewCardOut(**row)


@router.get("/cards/{vocab_id}", response_model=ReviewCardOut)
def get_card(vocab_id: int) -> ReviewCardOut:
    return _card_or_404(vocab_id)


@router.post("/cards/{vocab_id}/grade", response_model=ReviewCardOut)
def grade_card(vocab_id: int, payload: ReviewGradeIn) -> ReviewCardOut:
    """评一次分。返回评分后的卡 —— 前端据此把这张卡从队列里去掉。"""
    try:
        row = srs.grade(vocab_id, payload.grade, elapsed_ms=payload.elapsed_ms)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return ReviewCardOut(**row)


@router.post("/cards/{vocab_id}/suspend", response_model=ReviewCardOut)
def suspend_card(vocab_id: int) -> ReviewCardOut:
    """暂停复习这个词。专名、一次性词用得上 —— 它们不该反复占队列。

    只影响复习排期，生词本里那一条照旧在（`delete_vocab` 才是删）。
    """
    row = srs.set_suspended(vocab_id, True)
    if row is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return ReviewCardOut(**row)


@router.post("/cards/{vocab_id}/resume", response_model=ReviewCardOut)
def resume_card(vocab_id: int) -> ReviewCardOut:
    row = srs.set_suspended(vocab_id, False)
    if row is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return ReviewCardOut(**row)
