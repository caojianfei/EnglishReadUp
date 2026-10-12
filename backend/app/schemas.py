"""HTTP 层的数据契约（Pydantic v2）。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class ArticleCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2000, description="文章原始链接")
    force_render: bool = Field(default=False, description="强制走无头浏览器渲染")

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        s = v.strip()
        p = urlparse(s)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("必须是 http/https 开头的完整 URL")
        return s


class BlockOut(BaseModel):
    id: int
    order_index: int
    type: str
    text: str | None = None
    level: int | None = None
    is_stale: int = 0
    image_id: int | None = None
    origin_url: str | None = None
    local_path: str | None = None
    alt: str | None = None
    download_status: str | None = None
    media_url: str | None = None      # 已本地化图片的可直接渲染地址


class ArticleOut(BaseModel):
    id: int
    url: str
    canonical_url: str
    title: str | None = None
    source_domain: str | None = None
    sitename: str | None = None
    author: str | None = None
    published_at: str | None = None
    lang: str | None = None
    word_count: int | None = None
    cover_path: str | None = None
    cover_url: str | None = None
    extractor_pass: str | None = None
    status: str
    error_msg: str | None = None
    fetched_at: str | None = None
    created_at: str
    block_count: int = 0
    image_count: int = 0
    # 阅读进度（来自 article_progress，未读过则为空）
    last_block_id: int | None = None
    scroll_pct: float | None = None
    read_seconds: int = 0


class ProgressIn(BaseModel):
    """阅读进度上报。

    `read_seconds` 是**本次增量**而非累计值，服务端累加 ——
    这样客户端崩溃 / 重复提交都不会把总时长冲掉。
    """

    last_block_id: int | None = Field(default=None, description="当前视口最靠上的块 id")
    scroll_pct: float | None = Field(default=None, ge=0.0, le=1.0, description="滚动比例 0~1")
    read_seconds: int = Field(default=0, ge=0, le=86400, description="本次新增阅读秒数")


class ProgressOut(BaseModel):
    article_id: int
    last_block_id: int | None = None
    scroll_pct: float | None = None
    read_seconds: int = 0
    updated_at: str | None = None


class ArticleDetail(ArticleOut):
    blocks: list[BlockOut] = Field(default_factory=list)


class ArticleList(BaseModel):
    total: int
    items: list[ArticleOut]


class SearchHit(BaseModel):
    block_id: int
    article_id: int
    title: str | None = None
    url: str | None = None
    snip: str | None = None


class SearchResult(BaseModel):
    total: int
    items: list[SearchHit]


# ---------------------------------------------------------------- 分组检索（M4）

class BlockHit(BaseModel):
    article_id: int
    block_id: int
    article_title: str | None = None
    snippet: str | None = None
    start: int | None = None
    end: int | None = None


class VocabHit(BaseModel):
    id: int
    lemma: str
    surface: str | None = None
    phonetic: str | None = None
    pos: str | None = None
    meaning_cn: str | None = None
    meaning_in_ctx: str | None = None
    status: str = "new"
    worth: int = 1
    seen_count: int = 1
    sentence: str | None = None
    article_titles: str | None = None
    snippet: str | None = None


class AiHit(BaseModel):
    id: int
    article_id: int
    article_title: str | None = None
    prompt_type: str
    prompt_version: str
    model: str
    highlight_id: int | None = None
    block_id: int | None = None
    start: int | None = None
    end: int | None = None
    surface: str | None = None
    input_text: str = ""
    snippet: str | None = None
    created_at: str


class SearchGrouped(BaseModel):
    """三组结果。每条都带「跳回原文」所需的位置信息。"""

    q: str
    total: int
    blocks: list[BlockHit] = Field(default_factory=list)
    vocab: list[VocabHit] = Field(default_factory=list)
    ai: list[AiHit] = Field(default_factory=list)


# ---------------------------------------------------------------- 学习痕迹（M3）

class HighlightIn(BaseModel):
    """划词 / 选段。偏移锚定 blocks.text（红线 2）。"""

    block_id: int
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    kind: str | None = Field(
        default=None, description="word|phrase|sentence|paragraph；不传则由后端按选区推断"
    )
    surface: str | None = Field(default=None, max_length=6000, description="选中的原文（仅作校验）")
    color: str = Field(default="yellow", max_length=20)
    note: str | None = Field(default=None, max_length=2000)


class HighlightPatch(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, max_length=20)


class HighlightOut(BaseModel):
    id: int
    article_id: int
    block_id: int
    start_offset: int
    end_offset: int
    kind: str
    surface: str
    context_before: str | None = None
    context_after: str | None = None
    note: str | None = None
    color: str | None = None
    created_at: str
    block_type: str | None = None
    order_index: int | None = None
    preview: str | None = None


class HighlightList(BaseModel):
    total: int
    items: list[HighlightOut]


# ---------------------------------------------------------------- 词典（M3；M10 起本地化）

class AudioSlotOut(BaseModel):
    """一个口音的发音槽。音频一律本地化，前端只拿本机地址。"""

    url: str | None = Field(
        default=None, description="本机播放地址；None 表示该口音确认没有录音"
    )
    status: str = Field(default="pending", description="ok|pending|missing")
    origin: str | None = Field(default=None, description="youdao|wiktionary")
    accent: str | None = Field(
        default=None,
        description="实际拿到的口音（有道拿不到、回退到词典录音时可能不是请求的那个）",
    )
    ready: bool = Field(default=False, description="本地是否已有文件")


class WordEntryOut(BaseModel):
    """`GET /api/dict/{word}`：本地词条 + 英美发音槽 + 入册回执。

    契约里没有 `cached` / `error` 这类字段了 —— 查询是本地文件读，
    没有缓存可命中，失败也只有「没这个词」与「库没装」两种情况，都走 `source`。
    """

    word: str = Field(description="归一化后用于查询的词形")
    headword: str = Field(description="词典里的真实拼写（查 beijing 得到 Beijing）")
    lemma: str = Field(description="原形（划 preserved 时是 preserve）")
    pos: str = ""
    phonetic: str | None = Field(default=None, description="主音标：美音优先（全库唯一口径）")
    phonetic_uk: str | None = None
    phonetic_us: str | None = None
    phonetic_source: str = Field(default="missing", description="ipa|ecdict|missing")
    phonetic_of: str | None = Field(
        default=None, description="音标取自哪个原形（划 invading 时是 invade）"
    )
    meaning_cn: str = Field(default="", description="中文释义：有用户修正时就是修正值")
    meaning_cn_dict: str = Field(default="", description="词典原文（不受用户修正影响）")
    meaning_edited: bool = Field(default=False, description="中文释义是否用了用户修正")
    meaning_en: str = ""
    worth: int = Field(default=1, description="模型判定已下线，现按考纲标签/词频本地推导")
    tags: str = Field(default="", description="ECDICT 的考纲标签原样（zk gk cet4 …）")
    audios: dict[str, AudioSlotOut] = Field(
        default_factory=dict, description="按口音索引的发音槽：{'uk': …, 'us': …}"
    )
    source: str = Field(description="ecdict|missing|unavailable")
    setup: dict[str, Any] | None = Field(
        default=None,
        description="只在 `unavailable`（词典库没装）时给：自动补齐的状态与进度",
    )
    enrolled: dict[str, Any] | None = Field(
        default=None, description="本次是否把这个词收进了生词本"
    )


class MeaningOverrideIn(BaseModel):
    """`PUT /api/dict/override`：修正一个词条的中文释义（词放在 body 里，带斜杠也不怕）。"""

    word: str = Field(min_length=1, max_length=120)
    meaning_cn: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- AI（M3）

class SegmentIn(BaseModel):
    article_id: int
    text: str = Field(min_length=1, max_length=6000, description="待处理片段")
    block_id: int | None = Field(
        default=None, description="片段所属块；给了它且未给 context 时，后端自动取前后文"
    )
    context: str | None = Field(default=None, max_length=4000, description="前后文，供理解指代")
    highlight_id: int | None = None
    force: bool = False


class AiResultOut(BaseModel):
    prompt_type: str
    result: dict[str, Any]
    source: str = Field(description="llm | cache")
    model: str
    cached: bool
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    ai_log_id: int | None = None
    prompt_version: str
    enrolled: dict[str, Any] | None = Field(
        default=None, description="M4：本次结果落到学习痕迹表的回执（生词本/语法清单）"
    )
    warnings: list[str] = Field(default_factory=list)


class AiStreamIn(BaseModel):
    """SSE 版的一次 AI 调用（M5）。

    现在只有一种任务【解析】（explain）—— 划词查词典已经不再经过模型。
    `kind` 保留着是为了历史记录与 `ai.PROMPTS` 的校验口径统一，
    将来要加任务时在那里加，这里跟着改。
    """

    kind: str = Field(description="explain（界面上的【解析】）")
    article_id: int
    text: str = Field(min_length=1, max_length=6000)
    block_id: int | None = None
    context: str | None = Field(default=None, max_length=4000)
    highlight_id: int | None = None
    force: bool = False


class AiLogOut(BaseModel):
    id: int
    article_id: int
    highlight_id: int | None = None
    prompt_type: str
    prompt_version: str
    model: str
    input_text: str
    result: dict[str, Any]
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    source: str | None = None
    created_at: str


class AiLogList(BaseModel):
    total: int
    items: list[AiLogOut]


# ---------------------------------------------------------------- 生词本（M4）

class VocabOut(BaseModel):
    id: int
    lemma: str
    surface: str
    phonetic: str | None = None
    phonetic_uk: str | None = None
    phonetic_us: str | None = None
    phonetic_source: str | None = None
    phonetic_of: str | None = Field(
        default=None, description="音标取自哪个原形（划 invading 时是 invade）"
    )
    pos: str | None = None
    meaning_cn: str | None = None
    meaning_edited: bool = Field(default=False, description="释义是否用了用户修正（dict_overrides）")
    meaning_en: str | None = None
    meaning_in_ctx: str | None = None
    example: str | None = None
    note: str | None = None
    status: str = "new"
    worth: int = 1
    seen_count: int = 1
    article_id: int | None = None
    article_count: int = 0
    article_titles: str | None = None
    sentence: str | None = Field(default=None, description="文中划到该词的那一句")
    sentence_article_id: int | None = Field(
        default=None, description="上面那句出自哪篇文章（不是「首次入册那篇」）"
    )
    sentence_article_title: str | None = None
    # ---- 发音（M7；M10 起英美两槽）。音频一律本地化，前端只拿本机地址；
    # 生词本列表里两个喇叭都画，所以一次把两个槽都给全（见 vocab._SOURCES_SUB）。
    audios: dict[str, AudioSlotOut] = Field(
        default_factory=dict, description="按口音索引的发音槽：{'uk': …, 'us': …}"
    )
    sources: list[dict[str, Any]] | None = Field(
        default=None, description="只在单条详情里给：这个词在哪几篇、哪句里出现过"
    )
    # ---- M5 复习排期。服务端独写，前端只读（没有任何接口能改这几个字段）----
    srs_due: str | None = Field(default=None, description="下次到期，UTC ISO")
    srs_state: str = Field(default="new", description="new | learning | review")
    srs_interval: float = 0
    srs_reps: int = 0
    srs_lapses: int = 0
    srs_suspended: bool = False
    created_at: str
    updated_at: str | None = None


class VocabPatch(BaseModel):
    status: str | None = Field(default=None, description="new | learning | known")
    note: str | None = Field(default=None, max_length=2000)
    meaning_cn: str | None = Field(
        default=None, max_length=500, description="写入全局修正；空串 = 恢复词典原文"
    )
    worth: int | None = Field(default=None, description="1 值得记 / 0 不必记")


class VocabList(BaseModel):
    total: int
    items: list[VocabOut]


class VocabStats(BaseModel):
    total: int
    worth: int
    today: int
    by_status: dict[str, int]


# ---------------------------------------------------------------- 汇总清单（M4）

class SummaryOut(BaseModel):
    article: dict[str, Any]
    words: list[dict[str, Any]] = Field(default_factory=list)
    grammar: list[dict[str, Any]] = Field(default_factory=list)
    grammar_by_category: dict[str, int] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)


class GrammarPointOut(BaseModel):
    id: int
    article_id: int
    highlight_id: int | None = None
    category: str | None = None
    pattern: str | None = None
    explanation_cn: str
    source_sentence: str | None = None
    created_at: str


class GrammarList(BaseModel):
    total: int
    items: list[GrammarPointOut]


class GrammarPatch(BaseModel):
    category: str | None = Field(default=None, max_length=60)
    explanation_cn: str | None = Field(default=None, max_length=4000)


# ---------------------------------------------------------------- 间隔复习（M5）

class ReviewStateOut(BaseModel):
    """一张卡的复习排期。四个按钮上写的间隔也在这里（`intervals`）。"""

    state: str
    state_label: str = ""
    step: int = 0
    interval: float = 0
    ease: float = 2.5
    reps: int = 0
    lapses: int = 0
    due: str | None = None
    due_label: str = "—"
    last_at: str | None = None
    suspended: bool = False
    progress: str = ""


class ReviewCardOut(VocabOut):
    review: ReviewStateOut
    intervals: dict[str, str] = Field(
        default_factory=dict, description="again/hard/good/easy 各自会推到什么时候"
    )


class ReviewQueueOut(BaseModel):
    items: list[ReviewCardOut] = Field(default_factory=list)
    due_total: int = 0
    new_total: int = 0
    limit: int = 0
    new_limit: int = 0
    next_due: str | None = Field(default=None, description="当前队列之后最近的一个到期时间")
    next_after_queue: str | None = None


class ReviewGradeIn(BaseModel):
    grade: str = Field(description="again | hard | good | easy")
    elapsed_ms: int | None = Field(
        default=None, ge=0, le=3_600_000, description="这张卡看了多久，仅作统计"
    )


class ReviewStatsOut(BaseModel):
    total: int = 0
    due_now: int = 0
    new_available: int = 0
    suspended: int = 0
    by_state: dict[str, int] = Field(default_factory=dict)
    today: dict[str, int] = Field(default_factory=dict)
    retention_7d: float | None = None
    week_reviews: int = 0
    forecast: list[dict[str, Any]] = Field(default_factory=list)
    interval_cap_days: float = 365
    learn_steps_min: list[int] = Field(default_factory=list)


class ReviewLogOut(BaseModel):
    id: int
    vocab_id: int
    lemma: str | None = None
    grade: str
    prev_state: str
    next_state: str
    prev_interval: float | None = None
    next_interval: float | None = None
    ease_after: float
    was_lapse: int = 0
    due_after: str
    elapsed_ms: int | None = None
    created_at: str


class ReviewLogList(BaseModel):
    total: int
    items: list[ReviewLogOut]
