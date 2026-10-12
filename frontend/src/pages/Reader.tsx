/**
 * 阅读页。
 *
 * 三个承诺在这里落地：
 * 1. **离线可读**（M2）—— 正文来自 `/api/articles/{id}`（断网走 localStorage 缓存），
 *    图片来自后端 `/media`，全链路不碰原站。
 * 2. **刷新回原位**（M2）—— 位置优先用 `last_block_id` 还原（图片懒加载会把页面撑高，
 *    百分比不可靠），等图片加载完再校准几次；一旦用户自己滚了就立刻放弃校准。
 * 3. **划词即学**（M3）—— 划中一个词：立刻落库痕迹 + 查本地词典给音标释义；
 *    拖选一段：只弹【解析】按钮，**点了才落痕、才调模型**（避免误触白烧 token；
 *    「划了但没解析」的中间态也就不存在了 —— 没点过就当没划过）。
 *    高亮由「块内区间」重建，不写进正文数据。
 * 4. **等的时候看得见**（M5）—— 选段任务走 SSE，把模型正在吐的字实时画出来。
 *    解析冷启动要十几秒，光转圈会让人以为卡死；**结果本身没有任何变化**，
 *    流式只是把等待变得可解释（见 lib/stream.ts）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import AiPanel from '../components/AiPanel'
import type { AiRequest } from '../components/AiPanel'
import BlockRenderer, { TextWithHighlights, buildRenderItems } from '../components/BlockRenderer'
import SelectionPopover from '../components/SelectionPopover'
import Toolbar from '../components/Toolbar'
import { api, errorText } from '../lib/api'
import { formatDate, formatDuration, formatWords } from '../lib/prefs'
import type { ReadingPrefs } from '../lib/prefs'
import { groupByBlock, readSelection, rectOfOffsets } from '../lib/selection'
import { streamAi } from '../lib/stream'
import type {
  AiLog,
  AiLogList,
  AiResponse,
  AiTaskType,
  Highlight,
  SelectionInfo,
  WordEntry,
} from '../lib/types'
import { useReadingProgress } from '../lib/useReadingProgress'

/** 工具栏遮挡高度：还原位置时把这块减掉，否则目标行被压在工具栏下 */
const TOOLBAR_OFFSET = 84

/**
 * 「从别处跳过来定位」用的**合成**痕迹 id。
 *
 * 检索结果和汇总页的「划了没解析」都带 `?block=&start=&end=`，落到正文里
 * 需要一个能渲染成 `<mark>` 的区间。如果那个区间本来就有痕迹，就直接复用
 * 那条真实的（可点、可编辑）；没有才合成一条，用负数 id 与真实痕迹（id > 0）
 * 区分：它不进库、不可点（BlockRenderer 见到负 id 就不挂 onClick）。
 * 两者都会打上 `data-focus` 闪两下 —— 见 focusHighlight。
 */
const FOCUS_ID = -1

/** 取选中词所在的那一句，作为划词释义的语境。 */
function sentenceAround(info: SelectionInfo, text: string): string {
  const before = text.slice(0, info.start)
  const after = text.slice(info.end)
  const start = Math.max(
    before.lastIndexOf('.'),
    before.lastIndexOf('!'),
    before.lastIndexOf('?'),
    before.lastIndexOf('\n'),
  ) + 1
  const rel = after.search(/[.!?\n]/)
  const end = rel >= 0 ? info.end + rel + 1 : text.length
  return text.slice(start, end).trim().slice(0, 1200)
}

export default function Reader({
  prefs,
  onChange,
  isOffline,
}: {
  prefs: ReadingPrefs
  onChange: (patch: Partial<ReadingPrefs>) => void
  isOffline: boolean
}) {
  const { id: idParam } = useParams<{ id: string }>()
  const id = Number(idParam)
  const valid = Number.isInteger(id) && id > 0

  /**
   * 深链定位：`?block=&start=&end=` 从检索结果或汇总页的「划了没解析」过来。
   * 它的优先级高于「回到上次位置」—— 用户点的是「带我去看这里」，
   * 不是「接着上次读」，被自动还原劫走会很恼火。
   */
  const [params] = useSearchParams()
  const focus = useMemo(() => {
    const block = Number(params.get('block'))
    if (!Number.isFinite(block) || block <= 0) return null
    const start = Number(params.get('start'))
    const end = Number(params.get('end'))
    return {
      blockId: block,
      start: Number.isFinite(start) && start >= 0 ? start : null,
      end: Number.isFinite(end) && end > 0 ? end : null,
    }
  }, [params])

  const detail = useQuery({
    queryKey: ['article', id],
    queryFn: () => api.getArticle(id),
    enabled: valid,
    // 抓取中才轮询；done/failed 之后停掉
    refetchInterval: (q) => {
      const s = q.state.data?.status
      return s === 'pending' || s === 'fetching' ? 2500 : false
    },
  })

  const article = detail.data ?? null
  const ready = article?.status === 'done'

  // ---- 位置还原 ----
  const restoredRef = useRef(false)
  const [restored, setRestored] = useState(false)
  const [hint, setHint] = useState<string | null>(null)
  const [focusHint, setFocusHint] = useState(false)
  const targetRef = useRef<{ blockId: number | null; pct: number | null }>({
    blockId: null,
    pct: null,
  })
  const userMovedRef = useRef(false)
  /** 上一次由我们滚动到的位置，用来判断「页面是被人挪走了还是被图片顶偏了」 */
  const appliedRef = useRef(0)
  const [zoom, setZoom] = useState<{ src: string; caption: string | null } | null>(null)

  const applyRestore = useCallback((): boolean => {
    if (userMovedRef.current) return false
    const { blockId, pct } = targetRef.current
    if (blockId != null) {
      const el = document.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`)
      if (el) {
        const top = el.getBoundingClientRect().top + window.scrollY - TOOLBAR_OFFSET
        window.scrollTo({ top: Math.max(0, top) })
        appliedRef.current = window.scrollY
        return true
      }
    }
    if (pct != null && pct > 0) {
      const max = document.documentElement.scrollHeight - window.innerHeight
      if (max > 0) {
        window.scrollTo({ top: Math.max(0, pct * max) })
        appliedRef.current = window.scrollY
        return true
      }
    }
    return false
  }, [])

  useEffect(() => {
    const d = detail.data
    if (!d || restoredRef.current || d.status !== 'done') return
    restoredRef.current = true

    // 深链来了就以它为准，别再回到上次位置
    if (focus) {
      targetRef.current = { blockId: focus.blockId, pct: null }
      const raf = requestAnimationFrame(() => {
        applyRestore()
        setFocusHint(true)
        window.setTimeout(() => setFocusHint(false), 3600)
        setRestored(true)
      })
      return () => cancelAnimationFrame(raf)
    }

    const hasPos = d.last_block_id != null || (d.scroll_pct ?? 0) > 0.01
    targetRef.current = { blockId: d.last_block_id, pct: d.scroll_pct }
    if (!hasPos) {
      setRestored(true)
      return
    }

    const raf = requestAnimationFrame(() => {
      const ok = applyRestore()
      if (ok && d.scroll_pct) {
        setHint(`已回到上次位置 ${Math.round(d.scroll_pct * 100)}%`)
        window.setTimeout(() => setHint(null), 3200)
      }
      setRestored(true)
    })
    return () => cancelAnimationFrame(raf)
  }, [detail.data, applyRestore, focus])

  // 用户一开始自己滚，就不再校准 —— 否则图片加载完会把人拽回去
  useEffect(() => {
    const mark = () => {
      userMovedRef.current = true
    }
    window.addEventListener('wheel', mark, { passive: true })
    window.addEventListener('touchmove', mark, { passive: true })
    window.addEventListener('keydown', mark)
    return () => {
      window.removeEventListener('wheel', mark)
      window.removeEventListener('touchmove', mark)
      window.removeEventListener('keydown', mark)
    }
  }, [])

  // 图片陆续就位时反复校准。懒加载图片从 0 高度长出来会把后面的内容往下推，
  // 只还原一次的话位置必然偏。
  useEffect(() => {
    if (!restored || !ready) return
    const imgs = Array.from(document.querySelectorAll<HTMLImageElement>('article img'))
    const pending = imgs.filter((i) => !i.complete)
    if (pending.length === 0) return

    const reapply = () => {
      if (userMovedRef.current || !appliedRef.current) return
      // 图片插进来会改变内容高度，Chrome 的滚动锚定可能带出小幅位移 —— 那要校准。
      // 但偏移超过 150px 就只能是有人在滚了（滚轮之外，拖滚动条是不触发 wheel 的），
      // 这种情况必须收手，否则会把用户硬拽回原处。
      if (Math.abs(window.scrollY - appliedRef.current) > 150) {
        userMovedRef.current = true
        return
      }
      applyRestore()
    }
    pending.forEach((i) => i.addEventListener('load', reapply, { once: true }))
    const timers = [350, 1200, 2600].map((ms) => window.setTimeout(reapply, ms))
    return () => {
      pending.forEach((i) => i.removeEventListener('load', reapply))
      timers.forEach((t) => window.clearTimeout(t))
    }
  }, [restored, ready, applyRestore, detail.data])

  // ESC 关放大图 / 关面板
  useEffect(() => {
    if (!zoom) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setZoom(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [zoom])

  const progress = useReadingProgress(id, {
    enabled: valid && ready,
    suspended: !restored,
  })

  const blocks = article?.blocks ?? []
  /**
   * 标题块（M9）。
   *
   * 后端入库时把 `articles.title` 也写成一块（`ingest.title_block`），所以标题
   * 和正文共享同一套坐标系：划词、落痕迹、重建高亮、深链定位全都不用另开一条路。
   * 代价是它不能交给 BlockRenderer 遍历 —— 标题要排在页面顶部那个 header 里，
   * 所以那边会跳过 type='title'（见 buildRenderItems），这里自己画一次。
   */
  const titleBlock = useMemo(() => blocks.find((b) => b.type === 'title') ?? null, [blocks])
  const visibleBlocks = useMemo(
    () => (prefs.showImages ? blocks : blocks.filter((b) => b.type !== 'image' && b.type !== 'caption')),
    [blocks, prefs.showImages],
  )
  const items = useMemo(() => buildRenderItems(visibleBlocks), [visibleBlocks])

  // ================================================================ M3 学习痕迹

  const articleRef = useRef<HTMLElement>(null)

  /** 划词浮层的当前选区 */
  const [sel, setSel] = useState<{ info: SelectionInfo; rect: DOMRect } | null>(null)
  /** 词卡数据（本地词典查出来的）。与选段解析的 aiResp 是两条独立的线 */
  const [entry, setEntry] = useState<WordEntry | null>(null)
  const [wordBusy, setWordBusy] = useState(false)
  const [wordErr, setWordErr] = useState<string | null>(null)

  /** 本地新增/删除的痕迹：让高亮立刻出现或消失，不等列表接口回来 */
  const [added, setAdded] = useState<Highlight[]>([])
  const [removed, setRemoved] = useState<Set<number>>(new Set())

  /** AI 侧栏 */
  const [panel, setPanel] = useState<AiRequest | null>(null)
  const [aiResp, setAiResp] = useState<AiResponse | null>(null)
  const [aiBusy, setAiBusy] = useState(false)
  const [aiErr, setAiErr] = useState<string | null>(null)
  const [activeLogId, setActiveLogId] = useState<number | null>(null)
  /** 流式增量：模型正在吐的原始文本（每帧都是合法 JSON 的碎片，仅供「看见动静」） */
  const [aiPartial, setAiPartial] = useState('')
  const [aiThinking, setAiThinking] = useState(false)

  /**
   * 增量按帧节流。
   *
   * 一次 explain 实测能吐 150~400 个 chunk，每个都 `setState` 会让 React
   * 在一秒内重渲染几百次，正文里的图片也跟着抖动。攒到一个动画帧再刷一次，
   * 视觉上看不出差别，渲染次数却降到 60/s 以内。
   */
  const deltaBuf = useRef('')
  const deltaRaf = useRef<number | null>(null)
  const pushDelta = useCallback((piece: string) => {
    if (!piece) {
      // 空串是「上游在思考」的心跳，不进缓冲区，只翻一个标记
      setAiThinking(true)
      return
    }
    deltaBuf.current += piece
    if (deltaRaf.current != null) return
    deltaRaf.current = window.requestAnimationFrame(() => {
      deltaRaf.current = null
      setAiPartial(deltaBuf.current)
    })
  }, [])
  const clearDelta = useCallback(() => {
    deltaBuf.current = ''
    if (deltaRaf.current != null) {
      window.cancelAnimationFrame(deltaRaf.current)
      deltaRaf.current = null
    }
    setAiPartial('')
    setAiThinking(false)
  }, [])
  useEffect(() => clearDelta, [clearDelta])

  const qc = useQueryClient()
  /**
   * 清单页与阅读页读的是同一批痕迹与语法点。
   *
   * 全局 `staleTime` 是 30 秒：解析完立刻点【本篇清单】时，清单还挂着上次
   * 拉到的旧数据（用户看到的「得刷新一下才出现」）。痕迹/语法点一变就把
   * 清单缓存作废，点进去就是新的。
   */
  const refreshSummary = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['summary', id] })
  }, [qc, id])
  const hlQuery = useQuery({
    queryKey: ['highlights', id],
    queryFn: () => api.listHighlights(id),
    enabled: valid && ready,
  })
  const logQuery = useQuery({
    queryKey: ['aiLogs', id],
    queryFn: () => api.aiLogs(id, 60),
    enabled: valid && ready,
  })

  const highlights = useMemo(() => {
    const server = hlQuery.data?.items ?? []
    const serverIds = new Set(server.map((h) => h.id))
    return [
      ...server.filter((h) => !removed.has(h.id)),
      ...added.filter((h) => !serverIds.has(h.id) && !removed.has(h.id)),
    ]
  }, [hlQuery.data, added, removed])

  /**
   * 深链定位的目标痕迹。只给了 block 没给区间时就只滚过去，不标红。
   *
   * 优先**复用区间完全一致的真实痕迹**：它可点、可编辑，比叠一层合成标记好。
   * 找不到才合成一条 id = FOCUS_ID（负数）的：它不进库、不可点，刷新即消失。
   * 两条路都会在渲染层打上 `data-focus` 闪两下 —— 用户点的是「带我去看这里」，
   * 不管那个位置有没有划过敏，都得看见一眼。
   */
  const focusHighlight = useMemo<Highlight | null>(() => {
    if (!focus || focus.start == null || focus.end == null) return null
    const hit = highlights.find(
      (h) =>
        h.block_id === focus.blockId &&
        h.start_offset === focus.start &&
        h.end_offset === focus.end,
    )
    if (hit) return hit
    return {
      id: FOCUS_ID,
      article_id: id,
      block_id: focus.blockId,
      start_offset: focus.start,
      end_offset: focus.end,
      kind: 'phrase',
      surface: '',
      context_before: null,
      context_after: null,
      note: '定位到命中的位置',
      color: 'focus',
      created_at: '',
      block_type: null,
      order_index: null,
      preview: null,
    }
  }, [focus, id, highlights])

  const highlightsByBlock = useMemo(() => {
    const m = groupByBlock(highlights)
    if (!focusHighlight) return m
    const cur = m.get(focusHighlight.block_id) ?? []
    // 已经在库里了（上面复用的那条），不用再合成
    if (cur.some((h) => h.id === focusHighlight.id)) return m
    const next = new Map(m)
    next.set(focusHighlight.block_id, [...cur, focusHighlight])
    return next
  }, [highlights, focusHighlight])
  const blockText = useMemo(
    () => new Map(blocks.map((b) => [b.id, b.text ?? ''])),
    [blocks],
  )

  /**
   * 选区对应的**真实**痕迹。
   *
   * 合成定位标记（`FOCUS_ID`，负数）不算：它不进库，删它只会打一个必然失败的请求。
   */
  const highlightOf = useCallback(
    (info: SelectionInfo): Highlight | null =>
      highlightsByBlock
        .get(info.blockId)
        ?.find(
          (h) => h.id > 0 && h.start_offset === info.start && h.end_offset === info.end,
        ) ?? null,
    [highlightsByBlock],
  )

  /**
   * 把选区落成一条痕迹（幂等）。
   *
   * 划一段句子**不预落痕**：没点【解析】就当没划过 —— 正文里不留高亮、
   * 清单里不留「待解析」。解析时（或划单词时，见 handleMouseUp）才走这里补一条：
   * `ai_logs.highlight_id` 与清单里的语法点都要靠它指回原文那一句。
   * 落痕失败不阻塞调用方 —— 解释该给还是要给。
   */
  const ensureHighlight = useCallback(
    async (info: SelectionInfo): Promise<number | null> => {
      const existing = highlightOf(info)
      if (existing) return existing.id
      try {
        const h = await api.createHighlight(id, {
          block_id: info.blockId,
          start_offset: info.start,
          end_offset: info.end,
          kind: info.kind,
          surface: info.surface,
        })
        setAdded((prev) => [...prev.filter((x) => x.id !== h.id), h])
        setRemoved((prev) => {
          if (!prev.has(h.id)) return prev
          const next = new Set(prev)
          next.delete(h.id)
          return next
        })
        refreshSummary()
        return h.id
      } catch {
        return null
      }
    },
    [highlightOf, id, refreshSummary],
  )

  /**
   * 关浮层。
   *
   * 必须**连浏览器的原生选区一起收掉**：鼠标划出的那圈蓝色高亮是浏览器的选区，
   * 不是我们的状态 —— 只 `setSel(null)` 的话，点空白处或点浮层上的 ✕ 之后，
   * 那行字看起来仍然是「选中」状态（点空白处时浏览器也未必会自己收：
   * 落点若是 `user-select: none` 的元素，选区会原地保留）。
   * 用户报的「关闭了还是选中状态」就是这个。
   *
   * 清掉也不会误伤新手势：mousedown 的默认动作（把选区锚点落到按下处）
   * 在事件派发之后才执行，所以紧接着的拖选照常建立新的选区。
   */
  const closePopover = useCallback(() => {
    window.getSelection()?.removeAllRanges()
    setSel(null)
    setEntry(null)
    setWordErr(null)
    setWordBusy(false)
  }, [])

  /**
   * 查词卡。
   *
   * 本地词典查询是毫秒级的，所以卡片一次就出全（不再有「音标先出、释义后到」）。
   * 传 `articleId` 会让后端顺手把它收进生词本 —— 包括「点正文里划过的词重弹卡」
   * 这条路径（`seen_count` 只在同一篇里第一次遇见时才涨，见 vocab.enroll）。
   *
   * 失败只在网络层：本地查询没有超时、限流这些问题。
   */
  const loadWord = useCallback(
    async (info: SelectionInfo, highlightId: number | null) => {
      setWordBusy(true)
      setWordErr(null)
      try {
        const sentence = sentenceAround(info, blockText.get(info.blockId) ?? '')
        setEntry(
          await api.word(info.surface, {
            articleId: id,
            highlightId,
            sentence,
          }),
        )
      } catch (e) {
        setWordErr(errorText(e))
      } finally {
        setWordBusy(false)
      }
    },
    [id, blockText],
  )

  /**
   * 划词入口。
   *
   * 单词：落痕 + 查本地词典 —— 划中即完成，这是一次完整的动作（需求 5）。
   * 短语/句子：**只弹浮层**，痕迹与 AI 调用都等用户点【解析】（见 onPopoverAction）。
   * 划错了直接把浮层关掉就行，不会留下任何东西。
   * 落痕失败不阻塞查词：用户想看的是解释，不是数据库状态。
   */
  const handleMouseUp = useCallback(async () => {
    if (!ready) return
    const info = readSelection({
      getBlockText: (bid) => blockText.get(bid),
      container: articleRef.current,
    })
    if (!info) return

    const s = window.getSelection()
    if (!s || s.rangeCount === 0) return

    // 浮层位置按**信息里的区间**量，而不是浏览器的选区矩形：用户只划中词里
    // 几个字母时区间已被吸附到整词（selection.ts），拿原生选区量出来的浮层会
    // 贴着那半截字母飘，跟卡片上显示的词对不上。滚动时重定位走的也是这个函数，
    // 两处口径一致才不会「一滚就跳一下」。
    const el = articleRef.current?.querySelector<HTMLElement>(
      `[data-block-id="${info.blockId}"]`,
    )
    const rect = (el && rectOfOffsets(el, info.start, info.end)) || s.getRangeAt(0).getBoundingClientRect()
    if (rect.width === 0 && rect.height === 0) return

    // 高亮区间会改变 DOM，选区随之失效 —— 先把位置信息取出来再渲染
    setSel({ info, rect })
    setEntry(null)
    setWordErr(null)
    // 划了新的一段 = 注意力已经转移，别再让上一条痕迹顶着「正在查看」的蓝框
    setActiveLogId(null)

    // 只有单词在这里落痕。短语/句子点了【解析】才落（onPopoverAction）——
    // 于是「正文里有没有这条高亮」== 「这件事有没有发生过」。
    if (info.kind !== 'word') return

    const highlightId = await ensureHighlight(info)
    void loadWord(info, highlightId)
  }, [ready, blockText, ensureHighlight, loadWord])

  /**
   * 点正文里已经划过的**单词** → 重弹词卡。
   *
   * 不新建痕迹（那条高亮本来就在），只是按它的区间重新量一次位置、再查一遍
   * 本地词典。所以这条路径**不依赖任何历史记录** —— 以前要点开词卡得先找到
   * 那次 AI 调用的流水，记录被删掉就什么都没有了。
   */
  const openWordCard = useCallback(
    (h: Highlight) => {
      const el = articleRef.current?.querySelector<HTMLElement>(
        `[data-block-id="${h.block_id}"]`,
      )
      const rect = el ? rectOfOffsets(el, h.start_offset, h.end_offset) : null
      if (!rect) return
      const info: SelectionInfo = {
        blockId: h.block_id,
        start: h.start_offset,
        end: h.end_offset,
        surface: h.surface,
        kind: 'word',
        exact: true,
        snapped: false,
      }
      setSel({ info, rect })
      setEntry(null)
      setWordErr(null)
      void loadWord(info, h.id)
    },
    [loadWord],
  )

  /**
   * 调一次模型，结果进右侧面板。
   *
   * M5 起走 SSE：`done` 帧带回的结果与原来的 `POST /api/ai/<kind>` 逐字同构，
   * 所以面板、历史、入册回执全都不用改 —— 流式只是多了 `delta` 帧。
   *
   * 失败路径也一样分成两类（沿用原先的语义）：
   *   4xx/5xx → 后端在 `error` 帧里给了 detail，直接显示；
   *   fetch 本身抛了 → 是「连不上后端」，交给 catch 走 errorText。
   */
  const runAi = useCallback(
    async (req: AiRequest) => {
      // 历史上这里按记录类型分流（dict / translate / grammar / explain）。现在只有
      // 【解析】一种任务能跑：旧记录点了「重试」也统一用新版【解析】重跑一次
      // （那是这类按钮唯一能做的事，顺便完成一次升级）；而 **dict 类型根本不会
      // 走到这里** —— 划词已经改查本地词典，AiPanel 的重试按钮也只对 explain 显示。
      const kind: AiTaskType = 'explain'
      setPanel({ ...req, type: kind })
      setAiResp(null)
      setAiErr(null)
      setAiBusy(true)
      setActiveLogId(null)
      clearDelta()
      closePopover()
      try {
        await streamAi(
          {
            kind,
            article_id: id,
            text: req.text,
            block_id: req.blockId,
            highlight_id: req.highlightId,
          },
          {
            onDelta: pushDelta,
            onReset: clearDelta,
            onDone: (resp) => {
              setAiResp(resp)
              setActiveLogId(resp.ai_log_id)
              // 结果已经完整拿到，增量的使命结束 —— 留着会让用户
              // 在半截 JSON 和成品之间来回看
              clearDelta()
              // 解析会往语法清单里落点：清单缓存（30 秒鲜度）得作废，
              // 否则刚解析完点进【本篇清单】看到的还是上一次的结果
              refreshSummary()
            },
            onError: (status, detail) => {
              setAiErr(detail || `HTTP ${status}`)
              clearDelta()
            },
          },
        )
        void logQuery.refetch()
      } catch (e) {
        setAiErr(errorText(e))
        clearDelta()
      } finally {
        setAiBusy(false)
      }
    },
    [id, closePopover, logQuery, pushDelta, clearDelta, refreshSummary],
  )

  /**
   * 从浮层点【解析】：先补落痕迹，再调模型。
   *
   * 痕迹在点击这一刻才写库（划中句子不预落痕）—— 于是「有没有解析过」
   * 与「有没有这条高亮」是同一件事，汇总页不再需要「划了线但还没解析」这一栏。
   * 落痕失败也别拦着解析：结果先给用户，痕迹下次刷新才出现。
   */
  const onPopoverAction = useCallback(
    async (action: 'explain') => {
      if (!sel) return
      const { info } = sel
      const highlightId = await ensureHighlight(info)
      void runAi({
        type: action,
        text: info.surface,
        blockId: info.blockId,
        highlightId,
      })
    },
    [sel, ensureHighlight, runAi],
  )

  /**
   * 点正文里的高亮。
   *
   * 两路，按痕迹的类别分：
   *   word   → **重弹词卡**（本地词典当场查一次，不依赖历史记录）
   *   其余   → 打开右侧面板：找得到解析流水就回看，找不到就等用户点【解析】
   */
  const onPickHighlight = useCallback(
    (h: Highlight) => {
      if (h.kind === 'word') {
        openWordCard(h)
        return
      }
      const log = (logQuery.data?.items ?? []).find((l) => l.highlight_id === h.id)
      if (log) {
        // 历史记录转成与实时响应同构的形状，复用同一套渲染
        setPanel({
          type: log.prompt_type,
          text: log.input_text,
          blockId: null,
          highlightId: log.highlight_id,
        })
        setAiResp({
          prompt_type: log.prompt_type,
          result: log.result,
          source: 'cache',
          model: log.model,
          cached: true,
          tokens_in: 0,
          tokens_out: 0,
          latency_ms: log.latency_ms ?? 0,
          ai_log_id: log.id,
          prompt_version: log.prompt_version,
          warnings: [],
        })
        setActiveLogId(log.id)
      } else {
        // 划过但还没解析过：把选区信息放进面板，等用户点动作按钮
        setPanel({
          type: 'explain',
          text: h.surface,
          blockId: h.block_id,
          highlightId: h.id,
        })
        setAiResp(null)
        setActiveLogId(null)
      }
      setAiErr(null)
      setAiBusy(false)
      clearDelta()
    },
    [logQuery.data, clearDelta, openWordCard],
  )

  /**
   * 正文锚点跟着面板走。
   *
   * 点一条解析记录 = 「带我去看这条」：把对应的 `<mark>` 滚到视口中央，
   * 蓝框（data-active）随即落在它身上。找不到 mark 就退一步滚到它所在的块
   * （图片把内容顶偏时，至少让块进入视野）；两者都没有（记录关联的划线
   * 已经被删）就什么都不做 —— 没有目标可指。
   *
   * 滚动等一帧再量：点击会切换面板内容并触发重渲染，按新布局定位才不会偏。
   */
  const scrollToHighlight = useCallback(
    (highlightId: number | null) => {
      if (highlightId == null) return
      window.requestAnimationFrame(() => {
        const mark = articleRef.current?.querySelector<HTMLElement>(
          `mark[data-hl-id="${highlightId}"]`,
        )
        if (mark) {
          mark.scrollIntoView({ block: 'center', behavior: 'smooth' })
          return
        }
        const hl = highlights.find((h) => h.id === highlightId)
        if (!hl) return
        articleRef.current
          ?.querySelector<HTMLElement>(`[data-block-id="${hl.block_id}"]`)
          ?.scrollIntoView({ block: 'start', behavior: 'smooth' })
      })
    },
    [highlights],
  )

  const openLog = useCallback(
    (log: AiLog) => {
      setPanel({
        type: log.prompt_type,
        text: log.input_text,
        blockId: null,
        highlightId: log.highlight_id,
      })
      // 历史记录转成与实时响应同构的形状，复用同一套渲染
      setAiResp({
        prompt_type: log.prompt_type,
        result: log.result,
        source: 'cache',
        model: log.model,
        cached: true,
        tokens_in: 0,
        tokens_out: 0,
        latency_ms: log.latency_ms ?? 0,
        ai_log_id: log.id,
        prompt_version: log.prompt_version,
        warnings: [],
      })
      setAiErr(null)
      setAiBusy(false)
      setActiveLogId(log.id)
      clearDelta()
      scrollToHighlight(log.highlight_id)
    },
    [clearDelta, scrollToHighlight],
  )

  /** 取消标记：本地先撤掉（立刻见效），再删服务端 */
  const removeHighlight = useCallback(
    async (highlightId: number) => {
      setRemoved((prev) => new Set(prev).add(highlightId))
      setAdded((prev) => prev.filter((h) => h.id !== highlightId))
      closePopover()
      try {
        await api.deleteHighlight(highlightId)
        void hlQuery.refetch()
        refreshSummary()
      } catch {
        /* 删除失败：下次刷新会恢复，不做乐观回滚以免闪烁 */
      }
    },
    [closePopover, hlQuery, refreshSummary],
  )

  /**
   * 删掉一条解析记录（划词误操作的清理口）。
   *
   * 划词是「选中即翻译」，手一抖就会多一条，所以列表里每条都要能删。
   * 本地先摘掉再删服务端：等服务端返回才改的话，网络稍慢就是「点了没反应」。
   * 失败就把记录放回去（refetch）—— 它本来就还该在，顺便说明这次没删成。
   *
   * 记录删掉后，它独占的那处划线也一起撤：正文里那条高亮是以这条记录为
   * 解释的，记录没了它就是一处孤痕（还占着清单的统计）。
   * 判据是「没有别的记录还挂在同一高亮上」—— 重跑解析会给同一处划线再落一条
   * 记录，那种情况下高亮还得留着指路。真实的去留以后端为准（返回
   * highlight_deleted、随后重拉高亮列表），本地先按同一判据乐观预判。
   *
   * 删的恰好是面板里开着的那条时，面板退回空态但**不关闭**：
   * 内容已经失去凭据，而用户可能正在对比几条记录，顺手关掉面板太粗暴。
   */
  const removeLog = useCallback(
    async (log: AiLog) => {
      qc.setQueryData<AiLogList>(['aiLogs', id], (old) =>
        old
          ? {
              ...old,
              total: Math.max(0, old.total - 1),
              items: old.items.filter((l) => l.id !== log.id),
            }
          : old,
      )
      if (activeLogId === log.id) {
        setPanel({ type: 'explain', text: '', blockId: null, highlightId: null })
        setAiResp(null)
        setActiveLogId(null)
      }
      // 这条记录是否独占它的高亮（其他记录都还挂在别的高亮上）
      const hid = log.highlight_id
      const shared =
        hid != null &&
        (logQuery.data?.items ?? []).some((l) => l.id !== log.id && l.highlight_id === hid)
      const orphan = hid != null && !shared ? hid : null
      if (orphan != null) {
        setRemoved((prev) => new Set(prev).add(orphan))
        setAdded((prev) => prev.filter((h) => h.id !== orphan))
      }
      try {
        const res = await api.deleteAiLog(log.id, { withHighlight: orphan != null })
        // 以服务端返回为准收尾：它删掉了哪处高亮就补一次本地清理。
        // 二者理论上一致（本地已按「是否独占」预判），但记录列表只取了最近
        // 60 条，预判有可能和库里对不上，按返回值对齐才不会有残留。
        const gone = res.highlight_deleted
        if (gone != null) {
          setRemoved((prev) => new Set(prev).add(gone))
          setAdded((prev) => prev.filter((h) => h.id !== gone))
        }
        if (orphan != null || gone != null) {
          void hlQuery.refetch()
          refreshSummary()
        }
      } catch {
        if (orphan != null) {
          setRemoved((prev) => {
            const next = new Set(prev)
            next.delete(orphan)
            return next
          })
        }
        setHint('删除失败，请稍后重试')
        window.setTimeout(() => setHint(null), 3200)
        void logQuery.refetch()
      }
    },
    [qc, id, activeLogId, logQuery, hlQuery, refreshSummary],
  )

  /**
   * 浮层跟随正文。
   *
   * 这里踩过一个真坑（M3 浏览器验收实测）：浮层是 `position: fixed`，位置在
   * 划词那一刻就定死了，所以最初写成「一滚动就 setSel(null)」。但划完词页面
   * 常会自己动一下 —— 正文里十几张图片陆续加载把排版撑开，浏览器的滚动锚定
   * 随之调整 `scrollY` —— 结果浮层闪一下就消失，用户根本点不到，验收脚本也
   * 永远等不到它。
   *
   * 与其继续在「什么算用户真的滚了」上打补丁，不如换个思路：滚动时按
   * `blockId + 偏移` 重新量一次位置，浮层始终贴着那行字。只有当那行字真的
   * 滚出视口、或者块已经不在 DOM 里，才收起浮层。
   *
   * 依赖刻意只取「换了选区」这一组键，不依赖 `sel.rect` —— 否则每次
   * 更新位置都会重跑这个 effect，滚一下就把监听器和 rAF 重建一遍。
   */
  const selKey = sel ? `${sel.info.blockId}:${sel.info.start}-${sel.info.end}` : ''
  useEffect(() => {
    if (!sel || !selKey) return
    const { blockId, start, end } = sel.info
    let raf = 0

    const reposition = () => {
      raf = 0
      const el = articleRef.current?.querySelector<HTMLElement>(
        `[data-block-id="${blockId}"]`,
      )
      if (!el) {
        setSel(null)
        return
      }
      const rect = rectOfOffsets(el, start, end)
      if (!rect) {
        setSel(null)
        return
      }
      // 那行字滚出视口了，就没有理由继续显示
      if (rect.bottom < 0 || rect.top > window.innerHeight) {
        setSel(null)
        return
      }
      setSel((prev) => {
        if (!prev) return prev
        if (
          Math.abs(prev.rect.top - rect.top) < 1.5 &&
          Math.abs(prev.rect.left - rect.left) < 1.5
        ) {
          return prev // 位置没变就别重渲染
        }
        return { ...prev, rect }
      })
    }

    const onScroll = () => {
      if (!raf) raf = window.requestAnimationFrame(reposition)
    }

    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
      if (raf) window.cancelAnimationFrame(raf)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selKey])

  /**
   * 「正在查看的那条痕迹」不该永远亮着。
   *
   * `mark[data-active='1']` 那圈蓝框的含义是「右侧面板现在展示的就是这条」，
   * 但以前只有「换一条解析」（`activeLogId` 被重新赋值）才会移动它：关掉面板、
   * 点空白处、甚至划了新的一句，旧痕迹上的蓝框都还赖在正文里 —— 用户报的
   * 「点空白处选中框不会消失」就是它（截图里长句外面那圈蓝框）。
   *
   * 现在三种情形都收掉：
   *   ① 关面板（见下面 AiPanel 的 onClose）；
   *   ② 点了「别处」—— 不在痕迹上、不在面板或浮层里，等价于点空白处取消选中；
   *   ③ 划了新的一段 —— 注意力已经转移，浮层才是新的焦点（见 handleMouseUp）。
   *
   * 点痕迹本身不清：那是「打开这一条」，点了马上会把它设回来，清了反而闪。
   * 深链定位（focusHighlight）那圈也不受影响 —— 它是 URL 带来的「带我去看这里」，
   * 与面板无关（`activeHighlightId` 的兜底取值）。
   */
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (!t || typeof t.closest !== 'function') return
      if (t.closest('mark[data-hl-id]')) return
      if (t.closest('[data-testid="ai-panel"]')) return
      if (t.closest('[data-testid="selection-popover"]')) return
      setActiveLogId(null)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [])

  const cssVars = {
    '--er-fs': `${prefs.fontSize}px`,
    '--er-lh': String(prefs.lineHeight),
    '--er-width': `${prefs.width}px`,
  } as CSSProperties

  if (!valid) {
    return (
      <div className="p-10" style={{ background: 'var(--paper)', color: 'var(--ink)' }}>
        <p>链接不对。</p>
        <Link to="/" className="er-btn no-underline">
          返回列表
        </Link>
      </div>
    )
  }

  const logs = logQuery.data?.items ?? []
  const panelOpen = panel !== null
  /**
   * 正文里那圈蓝框（`mark[data-active='1']`）落在哪一条痕迹上。
   *
   * 提出来算一次是因为**标题和正文都要用**：面板正在看的那条痕迹可能划在标题上，
   * 两处各算一遍迟早会分叉。
   */
  const activeHighlightId =
    logs.find((l) => l.id === activeLogId)?.highlight_id ?? focusHighlight?.id ?? null

  return (
    <div
      className="min-h-screen"
      style={{ background: 'var(--paper)', color: 'var(--ink)' }}
    >
      <Toolbar
        prefs={prefs}
        onChange={onChange}
        pct={progress.pct}
        seconds={progress.seconds}
        isOffline={isOffline}
      />

      <div className="flex items-start">
        <main className="min-w-0 flex-1 px-5 pb-28 pt-10" style={cssVars}>
          {detail.isLoading ? <p className="er-muted text-sm">加载中…</p> : null}

          {detail.isError ? (
            <p className="text-sm text-red-600">
              读取失败：{(detail.error as Error).message}
            </p>
          ) : null}

          {article && !ready ? (
            <div className="mx-auto max-w-[600px] py-16 text-center">
              {article.status === 'failed' ? (
                <>
                  <p className="text-base text-red-600">抓取失败</p>
                  <p className="er-muted mt-2 text-sm">{article.error_msg}</p>
                  <p className="er-muted mt-4 text-xs">
                    可以在列表页点「重抓」，或勾选「强制浏览器渲染」后重新提交。
                  </p>
                </>
              ) : (
                <>
                  <p className="animate-pulse text-base">正在抓取正文与图片…</p>
                  <p className="er-muted mt-2 text-sm">
                    Sixth Tone 这类客户端渲染站点需要无头浏览器，约 40 秒。可以先去做别的。
                  </p>
                </>
              )}
              <Link to="/" className="er-btn mt-6 inline-block no-underline">
                返回列表
              </Link>
            </div>
          ) : null}

          {article && ready ? (
            <article className="reader-surface" ref={articleRef} onMouseUp={handleMouseUp}>
              <header className="mb-9">
                {/*
                 * 标题也是块（M9），挂上 data-block-id 后划词/高亮/深链三条路径
                 * 与正文完全同构。
                 *
                 * `prose-en` 是刻意加的：高亮底色、选区颜色这些规则都挂在这个作用域下
                 * （见 index.css 的 `.prose-en mark`）。标题要和正文一样能划、能高亮，
                 * 就得进同一个作用域，另起一套选择器只会越走越远。
                 */}
                <h1
                  className="blk-heading prose-en mt-0"
                  data-block-id={titleBlock?.id}
                  style={{ fontSize: '1.75em', lineHeight: 1.28 }}
                >
                  {titleBlock ? (
                    <TextWithHighlights
                      text={titleBlock.text ?? ''}
                      highlights={highlightsByBlock.get(titleBlock.id) ?? []}
                      activeId={activeHighlightId}
                      focusId={focusHighlight?.id ?? null}
                      onPick={onPickHighlight}
                    />
                  ) : (
                    article.title || '（无标题）'
                  )}
                </h1>
                <div className="er-muted mt-3 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs">
                  <span>{article.sitename || article.source_domain}</span>
                  {article.author ? (
                    <>
                      <span>·</span>
                      <span>{article.author}</span>
                    </>
                  ) : null}
                  {article.published_at ? (
                    <>
                      <span>·</span>
                      <span>{formatDate(article.published_at)}</span>
                    </>
                  ) : null}
                  <span>·</span>
                  <span>{formatWords(article.word_count)}</span>
                  <span>·</span>
                  <span>{article.image_count} 图</span>
                  {article.read_seconds > 0 ? (
                    <>
                      <span>·</span>
                      <span>累计阅读 {formatDuration(article.read_seconds)}</span>
                    </>
                  ) : null}
                  {highlights.length > 0 ? (
                    <>
                      <span>·</span>
                      <span>{highlights.length} 处标注</span>
                    </>
                  ) : null}
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <a
                    className="er-btn no-underline"
                    href={article.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    原文 ↗
                  </a>
                  <a className="er-btn no-underline" href={`/api/articles/${article.id}/markdown`}>
                    导出 MD
                  </a>
                  <Link
                    className="er-btn no-underline"
                    to={`/a/${article.id}/summary`}
                    data-testid="open-summary"
                  >
                    本篇清单
                  </Link>
                  <Link className="er-btn no-underline" to="/vocab">
                    生词本
                  </Link>
                  <Link className="er-btn no-underline" to="/search">
                    检索
                  </Link>
                  <button
                    className="er-btn"
                    onClick={() => window.scrollTo({ top: 0 })}
                  >
                    顶部
                  </button>
                  {!panelOpen ? (
                    <button
                      type="button"
                      className="er-btn"
                      onClick={() =>
                        setPanel({ type: 'explain', text: '', blockId: null, highlightId: null })
                      }
                    >
                      打开 AI 面板
                    </button>
                  ) : null}
                </div>
                <p className="er-muted mt-3 text-[11px]">
                  划中一个词 → 立刻给音标与释义；拖选一段 → 点【解析】看译文与句子拆解。
                  <span className="ml-2 inline-flex items-center gap-1 align-middle">
                    <span className="er-hl-swatch" data-kind="word" />
                    单词
                    <span className="er-hl-swatch ml-1" data-kind="sentence" />
                    短语 / 句子
                  </span>
                </p>
                <hr className="mt-7 border-0 border-t" style={{ borderColor: 'var(--rule)' }} />
              </header>

              <BlockRenderer
                items={items}
                onZoom={(src, caption) => setZoom({ src, caption })}
                highlightsByBlock={highlightsByBlock}
                activeHighlightId={activeHighlightId}
                focusHighlightId={focusHighlight?.id ?? null}
                onPickHighlight={onPickHighlight}
              />

              <footer className="mt-16 border-t pt-6" style={{ borderColor: 'var(--rule)' }}>
                <div className="er-muted flex flex-wrap items-center gap-3 text-xs">
                  <span>
                    本篇 {article.block_count} 块 · {article.image_count} 图 · 抓取方式{' '}
                    {article.extractor_pass || '—'}
                  </span>
                  <button className="er-btn" onClick={() => window.scrollTo({ top: 0 })}>
                    回到顶部
                  </button>
                  <Link to="/" className="er-btn no-underline">
                    返回列表
                  </Link>
                </div>
              </footer>
            </article>
          ) : null}
        </main>

        {panelOpen && ready ? (
          <AiPanel
            request={panel}
            response={aiResp}
            loading={aiBusy}
            error={aiErr}
            partial={aiPartial}
            thinking={aiThinking}
            logs={logs}
            activeLogId={activeLogId}
            width={prefs.panelWidth}
            onWidth={(w) => onChange({ panelWidth: w })}
            onClose={() => {
              // 关面板要连「正在查看的那条」一起清：面板都没了，
              // 正文里那圈蓝框就没有了存在的理由
              setPanel(null)
              setActiveLogId(null)
            }}
            // 只有【解析】能重试：历史里的 dict 记录已经无处可跑（划词不再经模型），
            // 硬给一个重试按钮等于给用户一条必然失败的路。
            onRetry={() => {
              if (panel && panel.type === 'explain') void runAi(panel)
            }}
            onOpenLog={openLog}
            onDeleteLog={removeLog}
          />
        ) : null}
      </div>

      {sel ? (
        <SelectionPopover
          info={sel.info}
          rect={sel.rect}
          entry={entry}
          loading={wordBusy}
          error={wordErr}
          // 单词划中即落痕；选段没点过【解析】就没有痕迹可取消 —— 浮层
          // 不该给出一个点了什么都不发生的按钮（见 SelectionPopover 的 marked）
          marked={sel.info.kind === 'word' || highlightOf(sel.info) != null}
          onAction={onPopoverAction}
          onDelete={() => {
            const hit = highlightOf(sel.info)
            if (hit) void removeHighlight(hit.id)
            else closePopover()
          }}
          onClose={closePopover}
          onEntryChange={setEntry}
        />
      ) : null}

      {hint ? (
        <div className="pointer-events-none fixed bottom-8 left-1/2 z-40 -translate-x-1/2">
          <div className="er-toast">{hint}</div>
        </div>
      ) : null}

      {focusHint && focus ? (
        <div
          className="pointer-events-none fixed bottom-8 left-1/2 z-40 -translate-x-1/2"
          data-testid="focus-hint"
        >
          <div className="er-toast">
            已定位到检索命中的位置{focusHighlight ? '（已标出）' : ''}
          </div>
        </div>
      ) : null}

      {zoom ? (
        <div
          className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/88 p-4"
          onClick={() => setZoom(null)}
          role="presentation"
        >
          <img
            src={zoom.src}
            alt={zoom.caption ?? ''}
            className="max-h-[86vh] max-w-full object-contain"
            onClick={(e) => e.stopPropagation()}
          />
          {zoom.caption ? (
            <p className="mt-4 max-w-[900px] text-center text-xs leading-relaxed text-white/75">
              {zoom.caption}
            </p>
          ) : null}
          <p className="mt-3 text-[11px] text-white/45">点击任意处关闭</p>
        </div>
      ) : null}
    </div>
  )
}
