/**
 * 后端 API 客户端。
 *
 * 三个刻意的设计：
 * 1. GET 走「网络优先 + localStorage 兜底」。图片与正文都已本地化在后端，
 *    所以断网时仍能读到上一次的内容 —— 这就是 M2 验收里的「离线可读」。
 *    注意 HTTP 4xx/5xx 属于真实错误，不会被兜底掩盖，只有网络层失败才降级。
 * 2. 进度上报是「增量 + keepalive」，页面卸载时也能送出去。
 * 3. AI 结果**不进** localStorage：后端已经按 input_hash 缓存，
 *    本地再存一份只会带来「后端换了 prompt 而前端还在用旧结果」的错位。
 */

import type {
  AiLogList,
  AiResponse,
  AiStats,
  Article,
  ArticleDetail,
  ArticleList,
  ExplainResult,
  GrammarList,
  GrammarPoint,
  Health,
  Highlight,
  HighlightList,
  HighlightPayload,
  Progress,
  ProgressPayload,
  ReviewCard,
  ReviewLogList,
  ReviewQueue,
  ReviewStats,
  SearchGrouped,
  SrsGrade,
  Summary,
  Vocab,
  VocabList,
  VocabStats,
  WordEntry,
} from './types'

const CACHE_PREFIX = 'er:cache:'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** 把后端返回的 `{"detail": "..."}` 变成能直接显示的句子。 */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    try {
      const parsed = JSON.parse(e.message) as { detail?: unknown }
      if (typeof parsed.detail === 'string') return parsed.detail
      if (Array.isArray(parsed.detail)) {
        // FastAPI 校验错误的 detail 是数组
        const first = parsed.detail[0] as { msg?: string } | undefined
        if (first?.msg) return first.msg
      }
    } catch {
      /* 不是 JSON，原样用 */
    }
    return e.message || `HTTP ${e.status}`
  }
  if (e instanceof Error) {
    // fetch 网络层失败
    if (/fetch|network|Failed to fetch/i.test(e.message)) return '连不上后端服务'
    return e.message
  }
  return String(e)
}

// ---------------------------------------------------------------- 离线状态

let offline = false
const offlineListeners = new Set<(v: boolean) => void>()

function setOffline(v: boolean) {
  if (v === offline) return
  offline = v
  offlineListeners.forEach((cb) => cb(v))
}

export function onOfflineChange(cb: (v: boolean) => void): () => void {
  offlineListeners.add(cb)
  return () => offlineListeners.delete(cb)
}

// ---------------------------------------------------------------- 缓存

type CacheRow<T> = { at: number; data: T }

function readCache<T>(key: string): CacheRow<T> | null {
  try {
    const raw = localStorage.getItem(CACHE_PREFIX + key)
    return raw ? (JSON.parse(raw) as CacheRow<T>) : null
  } catch {
    return null
  }
}

function writeCache<T>(key: string, data: T): void {
  try {
    localStorage.setItem(CACHE_PREFIX + key, JSON.stringify({ at: Date.now(), data }))
  } catch {
    /* 配额满了就算了，缓存不是关键路径 */
  }
}

/** 清掉某篇文章的缓存（删除文章后调用，避免缓存复活）。 */
export function dropCacheFor(articleId: number): void {
  const marker = `/api/articles/${articleId}`
  try {
    const keys = Object.keys(localStorage).filter((k) => {
      if (!k.startsWith(CACHE_PREFIX)) return false
      const url = k.slice(CACHE_PREFIX.length)
      // 必须精确到路径段，否则 /api/articles/1 会把 /api/articles/12 一起删掉
      return url === marker || url.startsWith(`${marker}/`) || url.startsWith(`${marker}?`)
    })
    keys.forEach((k) => localStorage.removeItem(k))
  } catch {
    /* ignore */
  }
}

async function cachedGet<T>(url: string): Promise<T> {
  const key = url
  try {
    const res = await fetch(url, { headers: { Accept: 'application/json' } })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      throw new ApiError(res.status, body.slice(0, 300) || res.statusText)
    }
    const data = (await res.json()) as T
    writeCache(key, data)
    setOffline(false)
    return data
  } catch (err) {
    if (err instanceof ApiError) throw err // 真实业务错误，不兜底
    const hit = readCache<T>(key)
    if (hit) {
      setOffline(true)
      return hit.data
    }
    throw err
  }
}

async function sendJSON<T>(url: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new ApiError(res.status, text.slice(0, 300) || res.statusText)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/**
 * 清掉某个词的查词缓存（释义修正 / 恢复之后调用）。
 *
 * `cachedGet` 把每次查词结果都留了一份，断网时拿它兜底 —— 不清的话，
 * 断网再划这个词还会弹出修正前的释义。两种入口（路径式 / `?word=`）都要清，
 * 带不带 `article_id` 等参数的变体也都算。
 */
export function dropDictCache(word: string): void {
  const markers = [`/api/dict/${encodeURIComponent(word)}`, `/api/dict?word=${encodeURIComponent(word)}`]
  try {
    const keys = Object.keys(localStorage).filter((k) => {
      if (!k.startsWith(CACHE_PREFIX)) return false
      const url = k.slice(CACHE_PREFIX.length).toLowerCase()
      return markers.some((m) => {
        const mk = m.toLowerCase()
        return url === mk || url.startsWith(`${mk}?`) || url.startsWith(`${mk}&`)
      })
    })
    keys.forEach((k) => localStorage.removeItem(k))
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------- 接口

export const api = {
  health: () => cachedGet<Health>('/api/health'),

  listArticles: (limit = 50, offset = 0) =>
    cachedGet<ArticleList>(`/api/articles?limit=${limit}&offset=${offset}`),

  getArticle: (id: number) => cachedGet<ArticleDetail>(`/api/articles/${id}`),

  createArticle: (url: string, forceRender = false) =>
    sendJSON<Article>('/api/articles', 'POST', { url, force_render: forceRender }),

  refetch: (id: number) => sendJSON<Article>(`/api/articles/${id}/refetch`, 'POST'),

  deleteArticle: (id: number) => sendJSON<void>(`/api/articles/${id}`, 'DELETE'),

  getProgress: (id: number) => cachedGet<Progress>(`/api/articles/${id}/progress`),

  putProgress: (id: number, payload: ProgressPayload) =>
    sendJSON<Progress>(`/api/articles/${id}/progress`, 'PUT', payload),

  search: (q: string, limit = 20) =>
    cachedGet<SearchGrouped>(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  suggestVocab: (q: string, limit = 8) =>
    sendJSON<{ items: { id: number; lemma: string; phonetic: string | null; meaning_cn: string | null }[] }>(
      `/api/search/suggest?q=${encodeURIComponent(q)}&limit=${limit}`,
      'GET',
    ),

  // -------------------------------------------------------------- M3 词典

  /**
   * 查一个词：英美音标 + 词性 + 中/英释义 + 发音槽状态，一次拿全。
   *
   * 纯本地查询（ECDICT + ipa-dict），毫秒级、断网可用 —— 走 `cachedGet`
   * 就是为了断网时还能命中 localStorage 里上次的结果（当电子词卡用）。
   *
   * 给了 `articleId` 时后端会顺手把它收进生词本（`enrolled` 是回执）；
   * 点正文里划过的词重弹词卡时也传，让「我又查了一次」这件事同样入册。
   * 词里带斜杠（`and/or`）时走 query 形式，避免路径分隔冲突。
   */
  word: (
    word: string,
    opts: { articleId?: number | null; highlightId?: number | null; sentence?: string | null; enroll?: boolean } = {},
  ) => {
    const qs = new URLSearchParams()
    if (opts.articleId != null) qs.set('article_id', String(opts.articleId))
    if (opts.highlightId != null) qs.set('highlight_id', String(opts.highlightId))
    if (opts.sentence) qs.set('sentence', opts.sentence)
    if (opts.enroll === false) qs.set('enroll', '0')
    const tail = qs.toString()
    return word.includes('/')
      ? cachedGet<WordEntry>(`/api/dict?word=${encodeURIComponent(word)}${tail ? `&${tail}` : ''}`)
      : cachedGet<WordEntry>(`/api/dict/${encodeURIComponent(word)}${tail ? `?${tail}` : ''}`)
  },

  /**
   * 纠正一个词条的中文释义。词典原文不动，修正叠在上面；生词本里同一个词一起改。
   * 词放在 body 里，带斜杠（`and/or`）也不用换入口。
   */
  setMeaning: (word: string, meaningCn: string) =>
    sendJSON<WordEntry>('/api/dict/override', 'PUT', { word, meaning_cn: meaningCn }),

  /** 删掉修正 = 恢复词典原文（生词本那一行一起恢复）。 */
  resetMeaning: (word: string) =>
    sendJSON<WordEntry>(`/api/dict/override?word=${encodeURIComponent(word)}`, 'DELETE'),

  // -------------------------------------------------------------- M3 痕迹

  listHighlights: (articleId: number, kind?: string) =>
    cachedGet<HighlightList>(
      `/api/articles/${articleId}/highlights${kind ? `?kind=${kind}` : ''}`,
    ),

  createHighlight: (articleId: number, payload: HighlightPayload) =>
    sendJSON<Highlight>(`/api/articles/${articleId}/highlights`, 'POST', payload),

  patchHighlight: (highlightId: number, payload: { note?: string; color?: string }) =>
    sendJSON<Highlight>(`/api/highlights/${highlightId}`, 'PATCH', payload),

  deleteHighlight: (highlightId: number) =>
    sendJSON<void>(`/api/highlights/${highlightId}`, 'DELETE'),

  // -------------------------------------------------------------- M3 AI

  /**
   * 【解析】。旧的 `/api/ai/translate`、`/api/ai/grammar` 已随三操作合并下线，
   * `/api/ai/dict`（划词释义）也在 M10 下线 —— 单词改查本地词典，不过模型。
   */
  aiExplain: (payload: SegmentPayload) =>
    sendJSON<AiResponse<ExplainResult>>(`/api/ai/explain`, 'POST', payload),

  aiLogs: (articleId: number, limit = 50) =>
    sendJSON<AiLogList>(`/api/ai/logs?article_id=${articleId}&limit=${limit}`, 'GET'),

  /**
   * 删掉一条解析记录（误操作清理）。
   *
   * 划词是「选中即翻译」，手一抖就会多一条。删的是这一次 AI 调用，缓存随记录
   * 一起消失（再划会重新调模型）。`withHighlight` 是阅读页面板用的：这条记录
   * 是它那处划线的最后一处引用时，把划线一并删掉 —— 记录删光后高亮只是一处
   * 没有解释的孤痕。检索页不传，保持旧语义：只清记录，不动原文标注。
   */
  deleteAiLog: (logId: number, opts: { withHighlight?: boolean } = {}) =>
    sendJSON<{ ok: boolean; highlight_deleted: number | null }>(
      `/api/ai/logs/${logId}${opts.withHighlight ? '?with_highlight=true' : ''}`,
      'DELETE',
    ),

  aiStats: () => sendJSON<AiStats>(`/api/ai/stats`, 'GET'),

  // -------------------------------------------------------------- M4 生词本

  listVocab: (params: VocabQuery = {}) => {
    const qs = new URLSearchParams()
    if (params.article_id != null) qs.set('article_id', String(params.article_id))
    if (params.status) qs.set('status', params.status)
    if (params.worth != null) qs.set('worth', String(params.worth))
    if (params.q) qs.set('q', params.q)
    if (params.sort) qs.set('sort', params.sort)
    qs.set('limit', String(params.limit ?? 100))
    qs.set('offset', String(params.offset ?? 0))
    return cachedGet<VocabList>(`/api/vocab?${qs.toString()}`)
  },

  vocabStats: () => cachedGet<VocabStats>('/api/vocab/stats'),

  getVocab: (id: number) => cachedGet<Vocab>(`/api/vocab/${id}`),

  patchVocab: (
    id: number,
    payload: { status?: string; note?: string; meaning_cn?: string; worth?: number },
  ) => sendJSON<Vocab>(`/api/vocab/${id}`, 'PATCH', payload),

  deleteVocab: (id: number) => sendJSON<void>(`/api/vocab/${id}`, 'DELETE'),

  // -------------------------------------------------------------- M4 语法清单

  listGrammar: (articleId?: number) =>
    cachedGet<GrammarList>(
      `/api/grammar${articleId != null ? `?article_id=${articleId}` : ''}`,
    ),

  patchGrammar: (id: number, payload: { category?: string; explanation_cn?: string }) =>
    sendJSON<GrammarPoint>(`/api/grammar/${id}`, 'PATCH', payload),

  deleteGrammar: (id: number) => sendJSON<void>(`/api/grammar/${id}`, 'DELETE'),

  // -------------------------------------------------------------- M4 汇总

  summary: (articleId: number) => cachedGet<Summary>(`/api/articles/${articleId}/summary`),

  // -------------------------------------------------------------- M5 复习

  /**
   * 今日队列。走 `cachedGet`：断网时还能看上次拉到的卡片与释义（当电子词卡用），
   * 只是评不了分 —— 评分是写操作，本来也不该离线成功。
   */
  reviewQueue: (limit = 60, newLimit = 20) =>
    cachedGet<ReviewQueue>(`/api/review/queue?limit=${limit}&new_limit=${newLimit}`),

  reviewStats: () => sendJSON<ReviewStats>(`/api/review/stats`, 'GET'),

  reviewCard: (vocabId: number) => cachedGet<ReviewCard>(`/api/review/cards/${vocabId}`),

  /** 评一次分。服务端返回评分**之后**的卡（含新的四档间隔）。 */
  reviewGrade: (vocabId: number, grade: SrsGrade, elapsedMs?: number) =>
    sendJSON<ReviewCard>(`/api/review/cards/${vocabId}/grade`, 'POST', {
      grade,
      elapsed_ms: elapsedMs ?? null,
    }),

  reviewSuspend: (vocabId: number) =>
    sendJSON<ReviewCard>(`/api/review/cards/${vocabId}/suspend`, 'POST'),

  reviewResume: (vocabId: number) =>
    sendJSON<ReviewCard>(`/api/review/cards/${vocabId}/resume`, 'POST'),

  reviewLogs: (vocabId?: number, limit = 50) =>
    sendJSON<ReviewLogList>(
      `/api/review/logs?limit=${limit}${vocabId != null ? `&vocab_id=${vocabId}` : ''}`,
      'GET',
    ),
}

export interface VocabQuery {
  article_id?: number | null
  status?: string
  worth?: number
  q?: string
  sort?: 'recent' | 'alpha' | 'priority' | 'seen'
  limit?: number
  offset?: number
}

/**
 * 导出走浏览器原生下载，不 fetch 进内存。
 *
 * 用 <a download> 而不是 fetch + Blob：后端已经设了 Content-Disposition
 * 带文件名，交给浏览器处理能保留同名重命名（`xxx (1).csv`）这类行为，
 * 也省掉把整份 CSV 读进 JS 内存再吐出去这一步。
 */
export function downloadExport(url: string, filename: string): void {
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

export function vocabExportUrl(params: { format?: 'anki' | 'md'; article_id?: number; status?: string } = {}): string {
  const qs = new URLSearchParams()
  qs.set('format', params.format ?? 'anki')
  if (params.article_id != null) qs.set('article_id', String(params.article_id))
  if (params.status) qs.set('status', params.status)
  return `/api/vocab/export?${qs.toString()}`
}

export function articleExportUrl(articleId: number, format: 'md' | 'anki' | 'text'): string {
  return `/api/articles/${articleId}/export?format=${format}`
}

export interface SegmentPayload {
  article_id: number
  text: string
  block_id?: number | null
  context?: string | null
  highlight_id?: number | null
  force?: boolean
}

/**
 * 卸载/切后台时送进度：`keepalive` 让请求在页面销毁后继续完成。
 * 这里刻意吞掉异常 —— 进度丢了不影响阅读。
 */
export function flushProgress(id: number, payload: ProgressPayload): void {
  try {
    void fetch(`/api/articles/${id}/progress`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch(() => undefined)
  } catch {
    /* ignore */
  }
}
