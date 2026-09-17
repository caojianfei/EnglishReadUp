"""全文检索接口（M4：三组结果 + 跳回原文的位置）。

旧契约（M2/M3）只搜正文块，返回 `{total, items}`。M4 把它换成三组：
正文 / 生词本 / AI 记录 —— 需求 5 说的是「AI 交互记录可全文检索」，
需求 6 说「生词可检索」，只搜正文等于三条要求只兑现了一条。

三组一起返回而不是分成三个端点：用户在搜索框里输一次，期望一次看到全部。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..schemas import SearchGrouped
from ..services import search as search_svc

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", response_model=SearchGrouped)
def search(
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(20, ge=1, le=100, description="每组各自的上限"),
) -> SearchGrouped:
    return SearchGrouped(**search_svc.search_all(q, limit=limit))


@router.get("/suggest")
def suggest(
    q: str = Query(..., min_length=1, max_length=60),
    limit: int = Query(8, ge=1, le=20),
) -> dict:
    """生词本前缀补全，供检索框的下拉用。"""
    return {"items": search_svc.suggest_vocab(q, limit=limit)}
