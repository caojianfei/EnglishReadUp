/** 与后端 `app/schemas.py` 一一对应的数据契约。改后端契约时同步改这里。 */

export type BlockType =
  /** 文章标题本身。以前它只在 `articles.title` 里，划不了词；现在也是块（M9） */
  | 'title'
  | 'heading'
  | 'para'
  | 'quote'
  | 'list'
  | 'image'
  | 'caption'
  | 'table'
  | 'video'
  | 'placeholder'

/** 正文块。`text` 是纯文本，也是划词偏移的坐标系（M3）。 */
export interface Block {
  id: number
  order_index: number
  type: BlockType
  text: string | null
  level: number | null
  is_stale: number
  image_id: number | null
  origin_url: string | null
  local_path: string | null
  alt: string | null
  download_status: string | null
  /** 已本地化图片的渲染地址：/media/{article_id}/{file} */
  media_url: string | null
}

export type ArticleStatus = 'pending' | 'fetching' | 'done' | 'failed'

export interface Article {
  id: number
  url: string
  canonical_url: string
  title: string | null
  source_domain: string | null
  sitename: string | null
  author: string | null
  published_at: string | null
  lang: string | null
  word_count: number | null
  cover_path: string | null
  cover_url: string | null
  extractor_pass: string | null
  status: ArticleStatus
  error_msg: string | null
  fetched_at: string | null
  created_at: string
  block_count: number
  image_count: number
  last_block_id: number | null
  scroll_pct: number | null
  read_seconds: number
}

export interface ArticleDetail extends Article {
  blocks: Block[]
}

export interface ArticleList {
  total: number
  items: Article[]
}

export interface Progress {
  article_id: number
  last_block_id: number | null
  scroll_pct: number | null
  read_seconds: number
  updated_at: string | null
}

export interface ProgressPayload {
  last_block_id?: number | null
  scroll_pct?: number | null
  /** 本次新增秒数（增量），服务端累加 */
  read_seconds?: number
}

export interface SearchGrouped {
  q: string
  total: number
  blocks: BlockHit[]
  vocab: VocabHit[]
  ai: AiHit[]
}

/** 正文命中。start/end 是块内字符区间，用来滚过去并闪一下。 */
export interface BlockHit {
  article_id: number
  block_id: number
  article_title: string | null
  snippet: string | null
  start: number | null
  end: number | null
}

export interface VocabHit {
  id: number
  lemma: string
  surface: string | null
  phonetic: string | null
  pos: string | null
  meaning_cn: string | null
  meaning_in_ctx: string | null
  status: VocabStatus
  worth: number
  seen_count: number
  sentence: string | null
  article_titles: string | null
  snippet: string | null
}

/** AI 流水命中。highlight 还在就带上块与区间，能跳回原文。 */
export interface AiHit {
  id: number
  article_id: number
  article_title: string | null
  prompt_type: AiPromptType
  prompt_version: string
  model: string
  highlight_id: number | null
  block_id: number | null
  start: number | null
  end: number | null
  surface: string | null
  input_text: string
  snippet: string | null
  created_at: string
}

export interface Health {
  status: string
  data_dir: string
  db: string
  fetching: number[]
  render_domains: string[]
  llm?: {
    provider: string
    model: string
    api_key_set: boolean
    prompt_version: string
  }
  /** 本地词典库（划词的音标与释义都来自它们）。只报「在不在、多大」。 */
  dict?: {
    ecdict: { path: string; exists: boolean; bytes: number }
    ipa: { files: Record<string, boolean>; entries: number | null }
    /** 启动自检 / 自动补齐的状态与进度 */
    bootstrap?: DictSetup & { items?: Record<string, unknown> }
  }
  audio?: {
    provider: string
    accent: string
    cached: number
    missing: number
    pending: number
    by_accent: Record<string, number>
    bytes: number
    breaker: boolean
    default_accent: string
  }
  vocab?: VocabStats
}

// ================================================================ M3：学习痕迹

export type HighlightKind = 'word' | 'phrase' | 'sentence' | 'paragraph'

/**
 * 一条划词/选段痕迹。
 *
 * 偏移锚定 `blocks.text`：`surface` 必定等于
 * `block.text.slice(start_offset, end_offset)`，高亮由区间重建。
 */
export interface Highlight {
  id: number
  article_id: number
  block_id: number
  start_offset: number
  end_offset: number
  kind: HighlightKind
  surface: string
  context_before: string | null
  context_after: string | null
  note: string | null
  color: string | null
  created_at: string
  block_type: string | null
  order_index: number | null
  preview: string | null
}

export interface HighlightList {
  total: number
  items: Highlight[]
}

export interface HighlightPayload {
  block_id: number
  start_offset: number
  end_offset: number
  kind?: HighlightKind | null
  surface?: string | null
  color?: string
  note?: string | null
}

// ================================================================ M3：词典

/** 发音的溯源信息。音频本身一律由后端本地化，前端只拿播放地址。 */
export interface AudioInfo {
  /** 本机播放地址（`/api/dict/{word}/audio`）；null = 确认没有录音 */
  audio: string | null
  /** ok 本地已有 · pending 还在下 · missing 确认没有 */
  audio_status?: string | null
  /** us | uk | other —— 回退源可能与请求的口音不同 */
  audio_accent?: string | null
  /** youdao | wiktionary */
  audio_origin?: string | null
  audio_ready?: boolean
}

/**
 * 一个口音的发音槽（M10）。
 *
 * 音频不做预热：`status` 是 `pending` 也照画喇叭，点下去后端才去取
 * （首次点会等几秒，取到之后就 `ok` 了）。`status === 'missing'` 是**终态**
 * ——确认这个口音没有录音，此时不画那个喇叭。
 */
export interface AudioSlot {
  /** 本机播放地址；null = 该口音确认没有录音 */
  url: string | null
  status: 'ok' | 'pending' | 'missing'
  /** 实际拿到的口音：回退到词典录音时可能不是请求的那个 */
  accent: string | null
  /** youdao | wiktionary */
  origin: string | null
  ready: boolean
}

/**
 * 离线词典数据的准备状态（ECDICT + ipa-dict）。
 *
 * 服务启动时会自检并在**后台**补齐缺失的数据，所以划词撞上「库没装」时
 * 多半不是「要你去装」，而是「正在下」—— `text` 就是给这种情况用的一句话。
 */
export interface DictSetup {
  /** ready 齐了 · downloading 在下 · error 下失败 · off 自动下载被关掉了 · idle 还没跑 */
  state: 'ready' | 'downloading' | 'error' | 'off' | 'idle'
  /** 后端拼好的一句话（含进度或失败原因），界面直接显示 */
  text: string
  /** 当前数据集下到百分之几；不在下载中就是 null */
  percent: number | null
  /** 还缺哪几个：ecdict | ipa_us | ipa_uk */
  missing: string[]
}

/**
 * `GET /api/dict/{word}` 的结果：本地词条 + 英美发音槽 + 入册回执。
 *
 * 音标与释义都来自本地词典库（ECDICT + ipa-dict），划词不再调模型 ——
 * 所以这里没有 `cached` / `latency` 这类字段，也不会有「模型名」。
 */
export interface WordEntry {
  /** 归一化后用于查询的词形 */
  word: string
  /** 词典里的真实拼写（查 beijing 得到 Beijing） */
  headword: string
  /** 原形（划 preserved 时是 preserve） */
  lemma: string
  pos: string
  /** 主音标：美音优先（生词本/汇总/检索都只显示这一条） */
  phonetic: string | null
  phonetic_uk: string | null
  phonetic_us: string | null
  /** ipa | ecdict | missing */
  phonetic_source: string
  /** 音标取自哪个原形（本地兜底路径命中时才有） */
  phonetic_of: string | null
  meaning_cn: string
  meaning_en: string
  worth: number
  /** ECDICT 的考纲标签原样（zk gk cet4 …） */
  tags: string
  /** 按口音索引：{'uk': …, 'us': …} */
  audios: Record<string, AudioSlot>
  /** ecdict 查到 / missing 未收录 / unavailable 词典库没装 */
  source: 'ecdict' | 'missing' | 'unavailable'
  /** 只在 `unavailable` 时给：自动补齐词典数据的进度/失败原因 */
  setup?: DictSetup | null
  enrolled?: { enrolled?: boolean; vocab_id?: number; lemma?: string; created?: boolean } | null
}

// 曾经还有一个 `Phonetic`（只查音标的那个端点）：M10 起查词一次拿全词条，
// 它被上面的 `WordEntry` 取代了。

// ================================================================ M3：AI

/** 划词释义结果。音标与发音字段只可能来自词典。 */
export interface DictResult extends AudioInfo {
  lemma: string
  pos: string
  meaning_cn: string
  meaning_in_ctx: string
  meaning_en: string
  example: string
  collocations: string[]
  register: string
  note: string
  /** 模型判定「值不值得记」：只影响排序与筛选，不影响是否收录 */
  worth?: number
  phonetic: string | null
  phonetic_uk: string | null
  phonetic_us: string | null
  phonetic_source?: string
  /** 音标取自哪个原形（划 invading 时是 invade） */
  phonetic_of?: string
}

/**
 * 【解析】结果（M8：原来并列的「翻译 / 精读解析 / 语法点」合成这一个动作）。
 *
 * 组织方式是「主干 → 挂靠」而不是按语法类别分栏：读长难句卡住的地方通常
 * 不是单词，而是「主干在哪、这坨东西挂在谁身上」。
 */
export interface ExplainResult {
  translation: string
  summary_cn: string
  /** 主干（句子骨架）；划到的只是一个短语时为空串 */
  skeleton: string
  /** 附加成分：每条都必须说清挂在哪个成分上 */
  parts: { text: string; role: string; attach: string; explain: string }[]
  key_phrases: { phrase: string; meaning_cn: string; note: string }[]
  /** 可复用的语法要点 —— 这一小节会自动收入语法清单 */
  grammar_points: {
    pattern: string
    category: string
    explain_cn: string
    evidence: string
    similar: string[]
  }[]
  difficulty: string
  /** 长难句的难点在哪；短句为空串 */
  hard_note: string
}

/** M3–M7 的历史 explain 形状，只在回看旧记录时出现。 */
export interface LegacyExplainResult {
  summary_cn: string
  translation: string
  structure: { part: string; text: string; note: string }[]
  clauses: { text: string; role: string; explain: string }[]
  key_phrases: { phrase: string; meaning_cn: string; note: string }[]
  grammar_notes: string[]
  difficulty: string
}

/** 历史任务「翻译」的结果形状，仅在回看旧流水时出现。 */
export interface TranslateResult {
  translation: string
  notes: { point: string; explain: string }[]
}

/** 历史任务「语法点」的结果形状，仅在回看旧流水时出现。 */
export interface GrammarResult {
  points: {
    pattern: string
    category: string
    explain_cn: string
    evidence: string
    similar: string[]
  }[]
}

/**
 * 现在的任务只有两类（M8 起）。
 *
 * translate / grammar 是 M3–M7 期间的历史任务：接口已随三操作合并下线，
 * 类型保留只为回看 ai_logs 里的旧记录 —— **别用它们发新请求**。
 */
export type AiTaskType = 'dict' | 'explain'
export type LegacyAiTaskType = 'translate' | 'grammar'
export type AiPromptType = AiTaskType | LegacyAiTaskType

export interface AiResponse<T = unknown> {
  prompt_type: AiPromptType
  result: T
  /** llm = 本次真的调用了模型；cache = 命中历史结果 */
  source: 'llm' | 'cache'
  model: string
  cached: boolean
  tokens_in: number
  tokens_out: number
  latency_ms: number
  ai_log_id: number | null
  prompt_version: string
  /** M4：本次结果落进学习痕迹表的回执（生词本 / 语法清单） */
  enrolled?: { enrolled?: boolean; vocab_id?: number; lemma?: string; created?: boolean; points?: number } | null
  warnings: string[]
}

export interface AiLog {
  id: number
  article_id: number
  highlight_id: number | null
  prompt_type: AiPromptType
  prompt_version: string
  model: string
  input_text: string
  result: Record<string, unknown>
  tokens_in: number | null
  tokens_out: number | null
  latency_ms: number | null
  source: string | null
  created_at: string
}

export interface AiLogList {
  total: number
  items: AiLog[]
}

export interface AiStats {
  ai_logs: number
  llm_calls: number
  cache_hits: number
  tokens_total: number
  highlights: number
  prompt_version: string
}

// ================================================================ 选区

/** 一次划词/选段的结果：块内偏移 + 原文。 */
export interface SelectionInfo {
  blockId: number
  start: number
  end: number
  surface: string
  /** 前端按选区形状推断的类别，后端会再复核一次 */
  kind: HighlightKind
  /** true = 偏移由 DOM 精确算出；false = 靠文本反查得到（跨节点选区时的兜底） */
  exact: boolean
  /**
   * true = 只选中了词内部的几个字母，区间已被扩到整个词（`snapToWholeWord`）。
   * 浮层据它说明「为什么显示的词比选中的长」。
   */
  snapped: boolean
}

// ================================================================ M4：生词本与汇总

export type VocabStatus = 'new' | 'learning' | 'known'

export const VOCAB_STATUS_LABEL: Record<VocabStatus, string> = {
  new: '生词',
  learning: '在学',
  known: '已掌握',
}

/**
 * 生词本条目。
 *
 * `lemma` 是词条（词典原形），`phonetic_of` 说明音标取自哪个原形 ——
 * 划 `invading` 时词条是 `invade`，音标也是 `invade` 的，两者一起看才不误导。
 */
export interface Vocab {
  id: number
  lemma: string
  surface: string
  phonetic: string | null
  phonetic_uk: string | null
  phonetic_us: string | null
  phonetic_source: string | null
  phonetic_of: string | null
  pos: string | null
  meaning_cn: string | null
  meaning_en: string | null
  meaning_in_ctx: string | null
  example: string | null
  note: string | null
  status: VocabStatus
  worth: number
  seen_count: number
  article_id: number | null
  article_count: number
  article_titles: string | null
  /** 文中划到该词的那一句（按文章筛时优先给那篇里的） */
  sentence: string | null
  /** 上面那句出自哪篇文章 —— 不等于 `article_id`（那是首次入册那篇） */
  sentence_article_id: number | null
  sentence_article_title: string | null
  // ---- 发音（M10 起英美两槽）：音频由后端本地化，前端只拿本机地址。
  //      列表里两个喇叭都画，所以状态一次给全；missing 的那个不画。
  audios: Record<string, AudioSlot>
  sources?: VocabSource[]
  // ---- M5 复习排期：服务端独写，前端只读（没有任何接口能改这几个字段）----
  srs_due: string | null
  srs_state: SrsState
  srs_interval: number
  srs_reps: number
  srs_lapses: number
  srs_suspended: boolean
  created_at: string
  updated_at: string | null
}

/** 一次出现：在哪篇、哪句里划到的。 */
export interface VocabSource {
  id: number
  article_id: number | null
  highlight_id: number | null
  surface: string
  sentence: string | null
  meaning_in_ctx: string | null
  created_at: string
  article_title: string | null
}

export interface VocabList {
  total: number
  items: Vocab[]
}

export interface VocabStats {
  total: number
  worth: number
  today: number
  by_status: Record<VocabStatus, number>
}

export interface GrammarPoint {
  id: number
  article_id: number
  highlight_id: number | null
  category: string | null
  pattern: string | null
  explanation_cn: string
  source_sentence: string | null
  created_at: string
}

export interface GrammarList {
  total: number
  items: GrammarPoint[]
}

/** 单篇汇总（需求 6）。数据全部由已有痕迹聚合而来。 */
export interface Summary {
  article: Article
  words: (Vocab & { sentence: string | null })[]
  grammar: GrammarPoint[]
  grammar_by_category: Record<string, number>
  stats: {
    words: number
    words_worth: number
    grammar: number
    highlights: number
  }
}

// ================================================================ M5：间隔复习

/** 四档评分。语义与 Anki 一致 —— 学习者的直觉本来就是这四档。 */
export type SrsGrade = 'again' | 'hard' | 'good' | 'easy'

export type SrsState = 'new' | 'learning' | 'review'

export const SRS_GRADES: SrsGrade[] = ['again', 'hard', 'good', 'easy']

export const SRS_GRADE_LABEL: Record<SrsGrade, string> = {
  again: '忘了',
  hard: '勉强',
  good: '记得',
  easy: '太简单',
}

/** 键盘快捷键：1~4 对应四档，与按钮顺序一致。 */
export const SRS_GRADE_KEY: Record<string, SrsGrade> = {
  '1': 'again',
  '2': 'hard',
  '3': 'good',
  '4': 'easy',
}

export const SRS_STATE_LABEL: Record<SrsState, string> = {
  new: '新词',
  learning: '学习中',
  review: '复习中',
}

/** 一张卡的复习排期。四个按钮上写的间隔也在 `intervals` 里。 */
export interface ReviewState {
  state: SrsState
  state_label: string
  step: number
  /** 当前间隔（天）；学习阶段为 0，真正的等待在分钟上 */
  interval: number
  ease: number
  reps: number
  lapses: number
  due: string | null
  /** 「下次到期」的人话版本，服务端算好 */
  due_label: string
  last_at: string | null
  suspended: boolean
  progress: string
}

export interface ReviewCard extends Vocab {
  review: ReviewState
  /** 四个档位各自会推到什么时候，直接渲染在按钮上 */
  intervals: Partial<Record<SrsGrade, string>>
}

export interface ReviewQueue {
  items: ReviewCard[]
  due_total: number
  new_total: number
  limit: number
  new_limit: number
  next_due: string | null
  next_after_queue: string | null
}

export interface ReviewStats {
  total: number
  due_now: number
  new_available: number
  suspended: number
  by_state: Record<SrsState, number>
  today: { reviews: number; again: number; lapses: number }
  /** 近 7 天「没点忘了」的比例，无数据为 null */
  retention_7d: number | null
  week_reviews: number
  forecast: { date: string; due: number }[]
  interval_cap_days: number
  learn_steps_min: number[]
}

export interface ReviewLog {
  id: number
  vocab_id: number
  lemma: string | null
  grade: SrsGrade
  prev_state: SrsState
  next_state: SrsState
  prev_interval: number | null
  next_interval: number | null
  ease_after: number
  /** 1 = 这次算「忘了」（只有 review 阶段点 again 才算） */
  was_lapse: number
  due_after: string
  elapsed_ms: number | null
  created_at: string
}

export interface ReviewLogList {
  total: number
  items: ReviewLog[]
}
