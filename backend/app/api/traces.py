"""学习痕迹接口：划词 / 选段的增删改查。

划词即落库（docs/DESIGN.md §7）：用户的每一次选择都是学习痕迹，
不需要「收藏」这个额外动作。重复划同一区间走幂等，不会堆垃圾记录。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from ..schemas import HighlightIn, HighlightList, HighlightOut, HighlightPatch
from ..services import highlights, ingest

router = APIRouter(prefix="/api", tags=["highlights"])


@router.post(
    "/articles/{article_id}/highlights",
    response_model=HighlightOut,
    status_code=201,
)
def create_highlight(article_id: int, payload: HighlightIn) -> HighlightOut:
    if ingest.get_article(article_id) is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    try:
        row = highlights.create_highlight(
            article_id,
            payload.block_id,
            payload.start_offset,
            payload.end_offset,
            kind=payload.kind,
            surface=payload.surface,
            color=payload.color,
            note=payload.note,
        )
    except highlights.HighlightError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return HighlightOut(**row)


@router.get("/articles/{article_id}/highlights", response_model=HighlightList)
def list_article_highlights(
    article_id: int,
    kind: str | None = Query(None, max_length=20),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> HighlightList:
    if ingest.get_article(article_id) is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    data = highlights.list_highlights(article_id, kind=kind, limit=limit, offset=offset)
    return HighlightList(total=data["total"], items=[HighlightOut(**r) for r in data["items"]])


@router.get("/highlights", response_model=HighlightList)
def list_all_highlights(
    kind: str | None = Query(None, max_length=20),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> HighlightList:
    """全局痕迹列表（M4 生词本 / 汇总页会吃这个）。"""
    data = highlights.list_highlights(None, kind=kind, limit=limit, offset=offset)
    return HighlightList(total=data["total"], items=[HighlightOut(**r) for r in data["items"]])


@router.get("/highlights/{highlight_id}", response_model=HighlightOut)
def get_highlight(highlight_id: int) -> HighlightOut:
    row = highlights.get_highlight(highlight_id)
    if row is None:
        raise HTTPException(status_code=404, detail="痕迹不存在")
    return HighlightOut(**row)


@router.patch("/highlights/{highlight_id}", response_model=HighlightOut)
def patch_highlight(highlight_id: int, payload: HighlightPatch) -> HighlightOut:
    row = highlights.update_highlight(
        highlight_id, note=payload.note, color=payload.color
    )
    if row is None:
        raise HTTPException(status_code=404, detail="痕迹不存在")
    return HighlightOut(**row)


@router.delete("/highlights/{highlight_id}", status_code=204)
def delete_highlight(highlight_id: int) -> Response:
    """删除痕迹。关联的 ai_logs 会保留（highlight_id 置空），学习历史不消失。"""
    if not highlights.delete_highlight(highlight_id):
        raise HTTPException(status_code=404, detail="痕迹不存在")
    return Response(status_code=204)
