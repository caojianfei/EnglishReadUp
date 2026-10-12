"""词典接口：本地词条查询 + 发音字节流。

    GET /api/dict/{word}          一次拿全词条（英美音标 + 词性 + 中/英释义 + 发音槽）
    GET /api/dict?word=...        等价入口。词里带斜杠（`and/or`）时用这个，避免路径分隔冲突
    GET /api/dict/{word}/audio    音频字节流，`?accent=uk|us`
    PUT /api/dict/override        修正一个词条的中文释义（同步生词本）
    DELETE /api/dict/override     删掉修正 = 恢复词典原文，`?word=`

这些端点**都不碰 LLM**（红线 3）：音标与释义来自本地词典库，音频由后端本地化后吐流。

为什么把音标与释义合成一个端点：
    以前是「`GET` 拿音标 + `POST /api/ai/dict` 拿释义」并行两个请求。释义本地化
    之后没有理由再拆两次 —— 两次往返、两个 loading 状态、两处都可能失败，
    换不来任何东西。

为什么这里带「入册」副作用：
    项目立场是「故意没有 `POST /api/vocab`」—— 入册是划词时自动发生的，不是用户
    点出来的。所以只要给了 `article_id`，就在这次查词里顺手把这个词收进生词本
    （幂等）。探针与验收脚本用 `?enroll=0` 关掉它。

入册用的是**原形**那份词条（`dictionary.lemma_entry`），不是这个端点返回的那份：
    端点给词卡用，口径是**词形优先**（划 `Experts` 给 `/ˈekspɜːts/`，见
    `services/dictionary.py`）；生词本按 `lemma` 建行、词头就是原形，音标与释义
    必须一起来自原形，否则会出现词头与音标对不上的行。两边只差一个数据来源，
    划线记录里的 `surface` 仍是用户真正划到的那个形态。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..db import tx
from ..schemas import MeaningOverrideIn, WordEntryOut
from ..services import audio as audio_service, dictionary, ecdict, ingest, overrides, vocab

log = logging.getLogger("english.api.dict")

router = APIRouter(prefix="/api/dict", tags=["dictionary"])

MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def _enroll(
    *,
    article_id: int,
    highlight_id: int | None,
    surface: str,
    data: dict[str, Any],
    sentence: str | None,
) -> dict[str, Any] | None:
    """把这次查词收进生词本（幂等）。

    `surface` 是用户**划到**的形态（`experts`），`data` 是**原形那份**词条
    （`expert`，见 `_entry`）。两者必须分开传：`vocab` 拿前者记「在哪儿划到的
    什么形态」，拿后者填音标与释义。

    入册在写事务里，而 `dictionary.lookup()` / `lemma_entry()` 的只读查询
    **已经结束**了 —— 不把只读查询叠进写事务（历史上踩过「写事务里再开一个
    事务」的死锁）。

    文章不存在时**只记日志、不报错**：入册是副作用，查词本身该照常返回。
    为此把 404 抛给用户，等于因为「生词本没记上」而把词卡也弄没了。
    """
    if ingest.get_article(article_id) is None:
        log.warning("入册跳过：文章 %s 不存在（词条仍照常返回）", article_id)
        return None
    try:
        with tx() as conn:
            return vocab.enroll(
                conn,
                article_id=article_id,
                highlight_id=highlight_id,
                word=surface,
                result=data,
                sentence=sentence or "",
            )
    except Exception as e:  # noqa: BLE001
        # 入册失败不能把已经查到的词条一起丢掉 —— 词卡是用户要的东西。
        log.warning("入册失败 %r: %s", surface, e)
        return None


def _entry(
    word: str,
    *,
    article_id: int | None,
    highlight_id: int | None,
    sentence: str | None,
    enroll: bool,
) -> WordEntryOut:
    data = dictionary.lookup(word)
    receipt = None
    if enroll and article_id is not None and data["source"] == "ecdict":
        receipt = _enroll(
            article_id=article_id,
            highlight_id=highlight_id,
            # 划到的形态与入库用的词条是两回事：前者进 vocab_sources.surface，
            # 后者（**原形**那份）填词头/音标/释义。两个 lookup 都是只读，
            # 都在 _enroll 的写事务之外。
            surface=data["word"],
            data=dictionary.lemma_entry(data),
            sentence=sentence,
        )
    return WordEntryOut(**data, enrolled=receipt)


@router.get("/{word}", response_model=WordEntryOut)
def lookup(
    word: str,
    article_id: int | None = Query(None, description="给了就顺手把这个词收进生词本"),
    highlight_id: int | None = Query(None, description="关联的划词痕迹（可选）"),
    sentence: str | None = Query(
        None, max_length=2000, description="该词所在的整句，存进生词本的「文中例句」"
    ),
    enroll: bool = Query(True, description="探针/验收用：置 0 关闭入册副作用"),
) -> WordEntryOut:
    return _entry(
        word,
        article_id=article_id,
        highlight_id=highlight_id,
        sentence=sentence,
        enroll=enroll,
    )


@router.get("", response_model=WordEntryOut)
def lookup_via_query(
    word: str = Query(..., min_length=1, max_length=120),
    article_id: int | None = Query(None),
    highlight_id: int | None = Query(None),
    sentence: str | None = Query(None, max_length=2000),
    enroll: bool = Query(True),
) -> WordEntryOut:
    """等价入口。词里带斜杠（如 `and/or`）时用这个，避免路径分隔冲突。"""
    return _entry(
        word,
        article_id=article_id,
        highlight_id=highlight_id,
        sentence=sentence,
        enroll=enroll,
    )


def _known(word: str) -> dict[str, Any]:
    """纠错只针对词典里**有**的词条：未收录的词不给补（只改释义，不造词条）。"""
    data = dictionary.lookup(word)
    if data["source"] != "ecdict":
        raise HTTPException(status_code=404, detail="词典未收录这个词，无法纠错")
    return data


@router.put("/override", response_model=WordEntryOut)
def put_override(payload: MeaningOverrideIn) -> WordEntryOut:
    """修正中文释义。词典原文不动，修正叠在上面；生词本里同一个词一起改。"""
    data = _known(payload.word)
    if not payload.meaning_cn.strip():
        raise HTTPException(status_code=422, detail="修正后的释义不能为空")
    # 只读查询已经结束，再开写事务（不把只读查询叠进 tx()）
    with tx() as conn:
        overrides.apply(conn, data["word"], payload.meaning_cn)
    return WordEntryOut(**dictionary.lookup(data["word"]))


@router.delete("/override", response_model=WordEntryOut)
def delete_override(word: str = Query(..., min_length=1, max_length=120)) -> WordEntryOut:
    """恢复词典原文：删掉修正，生词本里同一个词也恢复成词典释义。"""
    data = _known(word)
    with tx() as conn:
        overrides.clear(conn, data["word"], data["meaning_cn_dict"])
    return WordEntryOut(**dictionary.lookup(data["word"]))


@router.get("/{word}/audio", include_in_schema=False)
def audio(
    word: str,
    accent: str | None = Query(None, description="uk|us；缺省取 AUDIO_ACCENT（默认 uk）"),
    refresh: bool = Query(False, description="忽略本地已有文件，重新抓一次"),
) -> FileResponse:
    """发音音频。**本地优先，没有就当场下载**（音频不预热，这里是唯一的下载入口）。

    这一层同时是「隐蔽」的落点：流量被钳在服务端这一个出口，
    串行 + 间隔 + 抖动 + 熔断都在 `services/audio.py` 里，浏览器那侧看不到。
    本地已有文件时这里是纯磁盘读，不发任何外网请求。

    两个口音都能要：`type=1` 英音、`type=2` 美音（见 audio.YOUDAO_TYPE）。
    """
    norm = ecdict.normalize_word(word)
    if not norm:
        raise HTTPException(status_code=404, detail="没有这个词")
    acc = audio_service.norm_accent(accent)

    if refresh:
        audio_service.download(norm, acc, force=True)
    path, info = audio_service.ensure(norm, acc)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="暂无这个词的发音"
            if info.get("status") == "missing"
            else "发音还在取，稍后再点一次",
        )
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg"),
        headers={
            # 文件按内容哈希命名，同一个词的同一个口音不会变。留一天是因为万一以后
            # 从有道换成别的源，不至于让浏览器缓存压着旧录音不放。
            "Cache-Control": "public, max-age=86400",
            "X-Audio-Origin": str(info.get("origin") or ""),
            "X-Audio-Accent": str(info.get("accent") or acc),
        },
    )
