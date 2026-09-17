/**
 * 右侧 AI 面板。
 *
 * 等待体验分两层，M5 之后是第二层为主：
 *
 *   1. **阶段文案 + 秒数**（M3）：解析实测要 10~20 秒，单纯转圈会让人
 *      以为卡住了。按已耗时切换文案，把等待变得可解释。
 *   2. **实时增量**（M5，走 SSE）：把模型正在吐的文本直接画出来。
 *      内容是一段拼到一半的 JSON，**不面向阅读**，所以用等宽小字放在一个
 *      可滚动的框里 —— 它的唯一职责是证明「确实在动」。
 *      真结果一到（`done`）就让位给 `AiResultView`，半截 JSON 立刻消失。
 *
 * 宽度可调（M9）：把鼠标放到面板左边框上拖就行。宽度是阅读偏好的一部分
 * （「这台设备怎么显示」），所以照旧只落 localStorage，不进后端。
 */

import { useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'

import { PANEL_DEFAULT_W, clampPanelWidth } from '../lib/prefs'
import type { AiLog, AiPromptType, AiResponse, AiTaskType } from '../lib/types'
import AiResultView, { AI_LABEL } from './AiResultView'

/**
 * 阶段提示。按已耗时推进，纯提示性质。
 *
 * 顺序刻意与解析的产出顺序一致（译文 → 主干 → 附加成分 → 语法要点）：
 * 「只想看意思」的人看到第一句就能判断该不该继续等。
 */
const STAGES: Record<AiTaskType, string[]> = {
  dict: ['查词典取音标…', '组织释义…'],
  explain: ['生成译文…', '拆解主干与附加成分…', '提炼语法要点…'],
}

/** 历史任务（translate / grammar）没有阶段文案，回落到解析的那套。 */
function stagesOf(type: AiPromptType): string[] {
  return (STAGES as Record<string, string[]>)[type] ?? STAGES.explain
}

export interface AiRequest {
  type: AiPromptType
  text: string
  blockId: number | null
  highlightId: number | null
}

export default function AiPanel({
  request,
  response,
  loading,
  error,
  partial = '',
  thinking = false,
  logs,
  activeLogId,
  width,
  onWidth,
  onClose,
  onRetry,
  onOpenLog,
  onDeleteLog,
}: {
  request: AiRequest | null
  response: AiResponse | null
  loading: boolean
  error: string | null
  /** 流式增量（原始 JSON 碎片）。空串表示还没有正文 */
  partial?: string
  /** 上游在思考、正文还没开始 —— 用来把「一动不动」变成一句人话 */
  thinking?: boolean
  logs: AiLog[]
  activeLogId: number | null
  /** 面板宽度（px），来自阅读偏好 */
  width?: number
  /** 拖完松手时回调一次，把宽度写回偏好（拖动中不回调，见 startResize） */
  onWidth?: (w: number) => void
  onClose: () => void
  onRetry: () => void
  onOpenLog: (log: AiLog) => void
  onDeleteLog: (log: AiLog) => void
}) {
  const [elapsed, setElapsed] = useState(0)
  const [copied, setCopied] = useState(false)

  const baseWidth = clampPanelWidth(width ?? PANEL_DEFAULT_W)
  const [dragW, setDragW] = useState<number | null>(null)
  const [resizing, setResizing] = useState(false)
  /** 按下时的指针横坐标与面板宽度，拖动全程用它算新宽度 */
  const dragRef = useRef<{ x: number; w: number } | null>(null)

  // 拖动期间指针会压到正文上，光标得由 body 统一接管，否则一抖一抖
  useEffect(() => {
    if (!resizing) return
    document.body.classList.add('er-resizing')
    return () => document.body.classList.remove('er-resizing')
  }, [resizing])

  /**
   * 拖动中只改**本组件**的宽度，全程不碰偏好。
   *
   * 两个原因：偏好每变一次就往 localStorage 写一次，拖一趟要写几百次；更要紧的是
   * 偏好在 App 里，改它会让整个阅读页（几百个高亮节点）跟着重渲染，手感发涩。
   * 松手时才算落定，一次性交给 `onWidth`。
   */
  const startResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!onWidth || e.button !== 0) return
    e.preventDefault()
    // 指针捕获：拖到面板外面、甚至窗口外面，move/up 也照样回到这里
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { x: e.clientX, w: baseWidth }
    setResizing(true)
  }

  const moveResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragRef.current
    if (!start) return
    // 面板贴右边：指针往左 = 变宽
    setDragW(clampPanelWidth(start.w + (start.x - e.clientX)))
  }

  const endResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragRef.current
    if (!start) return
    dragRef.current = null
    setResizing(false)
    setDragW(null)
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
    const next = clampPanelWidth(start.w + (start.x - e.clientX))
    if (next !== baseWidth) onWidth?.(next)
  }

  useEffect(() => {
    if (!loading) {
      setElapsed(0)
      return
    }
    const t0 = Date.now()
    const timer = window.setInterval(() => setElapsed(Date.now() - t0), 200)
    return () => window.clearInterval(timer)
  }, [loading])

  useEffect(() => {
    setCopied(false)
  }, [response])

  const type = request?.type ?? 'explain'
  const stages = stagesOf(type)
  const stageIdx = Math.min(stages.length - 1, Math.floor(elapsed / 4000))
  const streaming = loading && partial.length > 0

  const copy = () => {
    const text =
      response && typeof response.result === 'object' && response.result
        ? JSON.stringify(response.result, null, 2)
        : ''
    if (!text) return
    void navigator.clipboard?.writeText(text).then(
      () => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1600)
      },
      () => undefined,
    )
  }

  return (
    <aside
      className="er-aside sticky top-[84px] flex h-[calc(100vh-84px)] shrink-0 flex-col"
      style={{ width: dragW ?? baseWidth }}
      data-testid="ai-panel"
    >
      {/* 左边缘拖拽条：跨在 border-left 上（左右各 4px），整条高度都能抓。
          双击复位 —— 拖窄了想一键还原时，不必再对准那个像素慢慢拉。 */}
      <div
        className="er-panel-resizer"
        data-drag={resizing ? '1' : undefined}
        data-testid="panel-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="拖动调整面板宽度"
        title="拖动调整宽度（双击复位）"
        onPointerDown={startResize}
        onPointerMove={moveResize}
        onPointerUp={endResize}
        onPointerCancel={endResize}
        onDoubleClick={() => onWidth?.(PANEL_DEFAULT_W)}
      />

      {/* 头部 */}
      <div className="flex items-start justify-between gap-2 px-3.5 py-3" style={{ borderBottom: '1px solid var(--rule)' }}>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">{AI_LABEL[type]}</span>
            {streaming ? (
              <span
                className="rounded px-1.5 py-0.5 text-[10px]"
                style={{ background: 'var(--code-bg)', color: 'var(--ink-faint)' }}
                data-testid="stream-badge"
              >
                流式
              </span>
            ) : null}
            {response ? (
              <span
                className="rounded px-1.5 py-0.5 text-[10px]"
                style={{ background: 'var(--code-bg)', color: 'var(--ink-faint)' }}
                data-testid="ai-source-badge"
                data-source={response.source}
                title={response.source === 'cache' ? '命中历史结果，未重复调用模型' : '本次调用模型生成'}
              >
                {response.source === 'cache' ? '缓存' : `${(response.latency_ms / 1000).toFixed(1)}s`}
              </span>
            ) : null}
          </div>
          {request?.text ? (
            <p className="mt-1 line-clamp-2 text-[11px] leading-snug" style={{ color: 'var(--ink-faint)' }}>
              {request.text.length > 90 ? `${request.text.slice(0, 90)}…` : request.text}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {response ? (
            <button type="button" className="er-btn !px-2 !py-1 !text-[11px]" onClick={copy}>
              {copied ? '已复制' : '复制'}
            </button>
          ) : null}
          <button
            type="button"
            className="rounded px-1.5 text-sm leading-6 transition-opacity hover:opacity-60"
            style={{ color: 'var(--ink-faint)' }}
            onClick={onClose}
            title="关闭面板"
          >
            ✕
          </button>
        </div>
      </div>

      {/* 主体 */}
      <div className="min-h-0 flex-1 overflow-auto px-3.5 py-3">
        {loading ? (
          <div className="flex flex-col py-2">
            <div className="flex items-center gap-2">
              <div
                className="h-4 w-4 shrink-0 animate-spin rounded-full"
                style={{ border: '2px solid var(--rule)', borderTopColor: 'var(--link)' }}
              />
              <p className="text-sm" style={{ color: 'var(--ink-soft)' }}>
                {streaming ? '正在生成…' : stages[stageIdx]}
              </p>
              <p className="ml-auto text-[11px]" style={{ color: 'var(--ink-faint)' }}>
                {(elapsed / 1000).toFixed(1)}s
              </p>
            </div>

            {streaming ? (
              <pre
                data-testid="ai-partial"
                className="mt-3 max-h-[46vh] overflow-auto whitespace-pre-wrap break-all rounded-md p-2.5 text-[11px] leading-relaxed"
                style={{ background: 'var(--code-bg)', color: 'var(--ink-soft)' }}
              >
                {partial}
              </pre>
            ) : thinking ? (
              <p
                data-testid="ai-thinking"
                className="mt-2 text-[11px]"
                style={{ color: 'var(--ink-faint)' }}
              >
                模型正在思考（这一步不产出正文）。
              </p>
            ) : (
              <p className="mt-2 text-[11px]" style={{ color: 'var(--ink-faint)' }}>
                {elapsed > 12000 ? '句子越长越慢，结果会缓存，下次秒回。' : '已连接，等待首字节…'}
              </p>
            )}
          </div>
        ) : null}

        {!loading && error ? (
          <div className="py-6 text-center">
            <p className="text-sm" style={{ color: 'var(--ink)' }}>
              {error}
            </p>
            {/* 只有【解析】能重跑：历史里的 dict 记录已经无处可跑（划词不再经模型），
                给一个必然失败的重试按钮不如不给。 */}
            {type === 'explain' ? (
              <button type="button" className="er-btn mt-3 !px-3 !py-1 !text-xs" onClick={onRetry}>
                重试
              </button>
            ) : null}
          </div>
        ) : null}

        {!loading && !error && response ? (
          <>
            <AiResultView type={response.prompt_type} result={response.result} />
            {/* 语法要点是自动入册的 —— 不告诉用户的话，「语法清单里凭多出一条」 */}
            {response.enrolled?.points ? (
              <p
                className="mt-3 text-[11px]"
                style={{ color: 'var(--ink-faint)' }}
                data-testid="enrolled-points"
              >
                {response.enrolled.points} 条语法要点已收入本篇清单
              </p>
            ) : null}
            {response.warnings.length ? (
              <p className="mt-3 text-[11px]" style={{ color: 'var(--ink-faint)' }}>
                {response.warnings.join('；')}
              </p>
            ) : null}
          </>
        ) : null}

        {!loading && !error && !response ? (
          <p className="py-6 text-center text-sm" style={{ color: 'var(--ink-faint)' }}>
            在正文里选中一段，点【解析】。
            <br />
            划中的
            <span style={{ color: 'var(--ink-soft)' }}>单词</span>
            不出现在这里 —— 它会直接弹出词卡（本地词典，即时）。
          </p>
        ) : null}
      </div>

      {/* 历史痕迹：需求 5「AI 协助过的内容全部可检索」的入口 */}
      <div style={{ borderTop: '1px solid var(--rule)' }}>
        <div className="flex items-center justify-between px-3.5 py-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--ink-faint)' }}>
            本篇解析记录
          </span>
          <span className="text-[11px]" style={{ color: 'var(--ink-faint)' }}>
            {logs.length}
          </span>
        </div>
        <div className="max-h-56 overflow-auto px-2 pb-2">
          {logs.length === 0 ? (
            <p className="px-1.5 pb-2 text-[11px]" style={{ color: 'var(--ink-faint)' }}>
              还没有记录。
            </p>
          ) : (
            logs.map((log) => (
              <div key={log.id} className="er-del-host" data-log-id={log.id}>
                <button
                  type="button"
                  className="er-log-item"
                  data-on={log.id === activeLogId ? '1' : undefined}
                  onClick={() => onOpenLog(log)}
                >
                  <span className="mr-1.5 font-medium" style={{ color: 'var(--link)' }}>
                    {AI_LABEL[log.prompt_type] ?? log.prompt_type}
                  </span>
                  <span className="text-[10px]" style={{ color: 'var(--ink-faint)' }}>
                    {log.source === 'cache' ? '缓存' : '生成'} · #{log.id}
                  </span>
                  <span className="mt-0.5 block truncate">{log.input_text.replace(/\s+/g, ' ').slice(0, 46)}</span>
                </button>
                {/* 解析有误操作（选长了、选错了）时留一个删除口，悬停才亮出来 */}
                <button
                  type="button"
                  className="er-log-del"
                  title="删除这条记录"
                  aria-label="删除这条记录"
                  data-testid="ai-log-delete"
                  onClick={() => onDeleteLog(log)}
                >
                  ✕
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    </aside>
  )
}
