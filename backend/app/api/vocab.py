"""生词本接口（M4，需求 6 的「生词表」）。

入册不是用户点出来的，是划词时自动发生的（`services/ai._post_persist` →
`services/vocab.enroll`）。所以这里全是「看与改」：列表、筛选、改状态与释义、
导出。**没有 POST /api/vocab** —— 让用户手工「加入生词本」意味着承认自动入册漏了词，
而需求 5 的立场是不漏。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from ..schemas import GrammarList, GrammarPatch, GrammarPointOut, VocabList, VocabOut, VocabPatch, VocabStats
from ..services import export, grammar, summarize, vocab

router = APIRouter(prefix="/api", tags=["vocab"])


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# ---------------------------------------------------------------- 生词本

@router.get("/vocab", response_model=VocabList)
def list_vocab(
    article_id: int | None = Query(None, description="只看某一篇里划过的词"),
    status: str | None = Query(None, description="new | learning | known"),
    worth: int | None = Query(None, ge=0, le=1, description="1 只看值得记的"),
    q: str | None = Query(None, max_length=60, description="词形 / 释义的子串"),
    sort: str = Query("recent", description="recent | alpha | priority | seen"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> VocabList:
    data = vocab.list_vocab(
        article_id=article_id, status=status, worth=worth, q=q,
        sort=sort, limit=limit, offset=offset,
    )
    return VocabList(total=data["total"], items=[VocabOut(**r) for r in data["items"]])


@router.get("/vocab/stats", response_model=VocabStats)
def vocab_stats() -> VocabStats:
    return VocabStats(**vocab.stats())


@router.get("/vocab/export", response_class=PlainTextResponse)
def vocab_export(
    format: str = Query("anki", pattern="^(anki|md)$"),
    article_id: int | None = None,
    status: str | None = None,
) -> Response:
    """生词本导出。默认 Anki CSV（`front,back,tags`）。

    **不写 BOM**：Anki 的 `#separator:` 指令必须出现在文件第一行的第一个字符，
    前面多个 `\\ufeff` 会让它认不出来，整份文件被当成普通笔记导进去。
    """
    rows = summarize.vocab_export_rows(article_id=article_id, status=status)
    if format == "md":
        body = export.render_vocab_markdown(rows)
        kind, ext = "text/markdown; charset=utf-8", "md"
    else:
        body = export.render_anki_csv(rows, tag_suffix="生词本")
        kind, ext = "text/csv; charset=utf-8", "csv"
    return PlainTextResponse(
        body,
        media_type=kind,
        headers={"Content-Disposition": f'attachment; filename="vocab-{_stamp()}.{ext}"'},
    )


@router.get("/vocab/{vocab_id}", response_model=VocabOut)
def get_vocab(vocab_id: int) -> VocabOut:
    row = vocab.get_vocab(vocab_id)
    if row is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return VocabOut(**row)


@router.patch("/vocab/{vocab_id}", response_model=VocabOut)
def patch_vocab(vocab_id: int, payload: VocabPatch) -> VocabOut:
    try:
        row = vocab.update_vocab(
            vocab_id,
            status=payload.status,
            note=payload.note,
            meaning_cn=payload.meaning_cn,
            worth=payload.worth,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return VocabOut(**row)


@router.delete("/vocab/{vocab_id}", status_code=204)
def delete_vocab(vocab_id: int) -> Response:
    """只删生词本这一行。原文高亮与 AI 流水都不动 —— 那是另一层痕迹。"""
    if not vocab.delete_vocab(vocab_id):
        raise HTTPException(status_code=404, detail="生词不存在")
    return Response(status_code=204)


# ---------------------------------------------------------------- 语法清单

@router.get("/grammar", response_model=GrammarList)
def list_grammar(
    article_id: int | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> GrammarList:
    data = grammar.list_points(article_id, limit=limit, offset=offset)
    return GrammarList(total=data["total"], items=[GrammarPointOut(**r) for r in data["items"]])


@router.patch("/grammar/{point_id}", response_model=GrammarPointOut)
def patch_grammar(point_id: int, payload: GrammarPatch) -> GrammarPointOut:
    try:
        row = grammar.update_point(
            point_id, category=payload.category, explanation_cn=payload.explanation_cn
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="语法点不存在")
    return GrammarPointOut(**row)


@router.delete("/grammar/{point_id}", status_code=204)
def delete_grammar(point_id: int) -> Response:
    if not grammar.delete_point(point_id):
        raise HTTPException(status_code=404, detail="语法点不存在")
    return Response(status_code=204)
