"""文章接口：提交抓取、列表、详情、重抓、删除、导出、阅读进度。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from ..schemas import (
    ArticleCreate,
    ArticleDetail,
    ArticleList,
    ArticleOut,
    ProgressIn,
    ProgressOut,
    SummaryOut,
)
from ..services import export, ingest, jobs, summarize

router = APIRouter(prefix="/api/articles", tags=["articles"])


def _strip(art: dict) -> dict:
    """过滤掉库表里的内部字段，交给 Pydantic 校验。"""
    allowed = set(ArticleOut.model_fields)
    return {k: v for k, v in art.items() if k in allowed}


@router.post("", response_model=ArticleOut, status_code=201)
def create_article(payload: ArticleCreate) -> ArticleOut:
    """提交一个 URL。

    已抓取成功过的文章直接返回（幂等）；失败或从未抓取的才会入队。
    """
    article_id, created = ingest.get_or_create_article(payload.url)
    art = ingest.get_article(article_id)
    if art is None:                       # pragma: no cover - 理论上不可能
        raise HTTPException(status_code=500, detail="文章创建失败")

    if created or art["status"] in ("failed", "pending"):
        jobs.submit_fetch(article_id, force_render=payload.force_render)
        art = ingest.get_article(article_id)
    return ArticleOut(**_strip(art))


@router.get("", response_model=ArticleList)
def list_articles(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=200, description="按标题或 URL 模糊过滤"),
) -> ArticleList:
    data = ingest.list_articles(limit=limit, offset=offset, q=q)
    return ArticleList(total=data["total"], items=[ArticleOut(**_strip(a)) for a in data["items"]])


@router.get("/{article_id}", response_model=ArticleDetail)
def get_article(article_id: int) -> ArticleDetail:
    art = ingest.get_article(article_id)
    if art is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    return ArticleDetail(**_strip(art), blocks=ingest.get_blocks(article_id))


@router.post("/{article_id}/refetch", response_model=ArticleOut)
def refetch(article_id: int, force_render: bool = False) -> ArticleOut:
    art = ingest.get_article(article_id)
    if art is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    if not jobs.submit_fetch(article_id, force_render=force_render):
        raise HTTPException(status_code=409, detail="该文章正在抓取中")
    return ArticleOut(**_strip(ingest.get_article(article_id)))


@router.delete("/{article_id}", status_code=204)
def delete_article(article_id: int) -> Response:
    if not ingest.delete_article(article_id):
        raise HTTPException(status_code=404, detail="文章不存在")
    return Response(status_code=204)


@router.get("/{article_id}/markdown", response_class=PlainTextResponse)
def export_markdown(article_id: int) -> PlainTextResponse:
    art = ingest.get_article(article_id)
    if art is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    md = export.render_markdown(art, ingest.get_blocks(article_id), media_prefix=f"/media/{article_id}")
    name = f"article-{article_id}.md"
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ------------------------------------------------------- 汇总清单与导出（M4）

@router.get("/{article_id}/summary", response_model=SummaryOut)
def article_summary(article_id: int) -> SummaryOut:
    """本篇的生词表 + 语法清单（需求 6）。数据全部来自已有的学习痕迹，不再调模型。"""
    try:
        data = summarize.build(article_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return SummaryOut(**data)


@router.get("/{article_id}/export", response_class=PlainTextResponse)
def article_export(
    article_id: int,
    format: str = Query("md", pattern="^(md|anki|text)$"),
) -> Response:
    """导出。

    - `md`   汇总清单（生词表 + 语法清单），拿去打印或贴进笔记
    - `anki` 生词本的 Anki CSV（`front,back,tags`）
    - `text` 整篇原文 Markdown（同 `/markdown`，收在这里让三种导出只有一个入口）
    """
    try:
        data = summarize.build(article_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    slug = f"article-{article_id}"
    if format == "anki":
        rows = summarize.vocab_export_rows(article_id=article_id)
        body = export.render_anki_csv(rows, tag_suffix=slug)
        media, ext = "text/csv; charset=utf-8", "csv"
    elif format == "text":
        art = ingest.get_article(article_id)
        body = export.render_markdown(art, ingest.get_blocks(article_id),
                                      media_prefix=f"/media/{article_id}")
        media, ext = "text/markdown; charset=utf-8", "md"
    else:
        body = export.render_summary_markdown(data)
        media, ext = "text/markdown; charset=utf-8", "md"

    return PlainTextResponse(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{slug}-summary.{ext}"'},
    )


# ---------------------------------------------------------------- 阅读进度（M2）
@router.get("/{article_id}/progress", response_model=ProgressOut)
def get_progress(article_id: int) -> ProgressOut:
    if ingest.get_article(article_id) is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    row = ingest.get_progress(article_id)
    if row is None:                       # 从未读过：返回空进度而非 404
        return ProgressOut(article_id=article_id)
    return ProgressOut(**row)


@router.put("/{article_id}/progress", response_model=ProgressOut)
def put_progress(article_id: int, payload: ProgressIn) -> ProgressOut:
    """上报阅读进度。前端定期调用；`read_seconds` 是增量，服务端累加，故可重复提交。"""
    if ingest.get_article(article_id) is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    row = ingest.save_progress(
        article_id,
        last_block_id=payload.last_block_id,
        scroll_pct=payload.scroll_pct,
        add_seconds=payload.read_seconds,
    )
    return ProgressOut(**row)
