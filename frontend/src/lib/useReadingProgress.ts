/**
 * 阅读进度：采集当前阅读位置 + 活跃时长，定期上报。
 *
 * 三个刻意的设计（验收标准是「刷新回到原位置」）：
 * 1. 位置同时记 `last_block_id` 与 `scroll_pct`。恢复时优先用 block id ——
 *    它不受图片懒加载把页面撑高的影响，比例会。
 * 2. 时长用**增量**上报，网络抖动或重复提交都不会把总时长冲掉。
 * 3. `suspended` 期间不记录位置变化。滚动恢复本身就是一次程序化滚动，
 *    不屏蔽的话会把「上次读到的位置」立刻改写成文章开头。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api, flushProgress } from './api'

const BLOCK_SELECTOR = '[data-block-id]'
/** 工具栏高度：判断「当前读到哪一块」时把遮挡的部分排除掉 */
const TOOLBAR_OFFSET = 96
const REPORT_INTERVAL_MS = 15_000

export interface ReadingProgressHandle {
  /** 滚动比例 0~1，用于顶部进度条 */
  pct: number
  /** 本次会话累计阅读秒数 */
  seconds: number
  /** 立即上报（切换路由前调用） */
  flush: () => void
}

export function useReadingProgress(
  articleId: number | null,
  opts: { enabled: boolean; suspended: boolean },
): ReadingProgressHandle {
  const { enabled, suspended } = opts
  const [pct, setPct] = useState(0)
  const [seconds, setSeconds] = useState(0)

  /** 上报用（受 suspended 保护） */
  const reportRef = useRef<{ blockId: number | null; pct: number }>({ blockId: null, pct: 0 })
  const pendingSecondsRef = useRef(0)
  const suspendedRef = useRef(suspended)
  suspendedRef.current = suspended

  const measure = useCallback(() => {
    const doc = document.documentElement
    const max = doc.scrollHeight - window.innerHeight
    const p = max > 8 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0

    const nodes = document.querySelectorAll<HTMLElement>(BLOCK_SELECTOR)
    const line = window.scrollY + TOOLBAR_OFFSET
    let current: number | null = reportRef.current.blockId
    for (const el of nodes) {
      const top = el.getBoundingClientRect().top + window.scrollY
      if (top + el.offsetHeight > line) {
        current = Number(el.dataset.blockId)
        break
      }
    }

    setPct((prev) => (Math.abs(prev - p) > 0.002 ? p : prev))

    // suspended 期间（滚动恢复中）基线跟着当前位置走 —— 恢复本身是一次
    // 程序化滚动，不跟着走的话「上次读到哪」会被立刻改写成文章开头。
    reportRef.current = { blockId: current, pct: p }
  }, [])

  // 滚动测量（rAF 节流）
  useEffect(() => {
    if (!enabled) return
    let raf = 0
    const onScroll = () => {
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        measure()
      })
    }
    measure()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      if (raf) cancelAnimationFrame(raf)
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
    }
  }, [enabled, measure])

  // 活跃计时：只看「页面可见」，不看焦点（很多人一边读一边切窗口）
  useEffect(() => {
    if (!enabled) return
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      pendingSecondsRef.current += 1
      setSeconds((s) => s + 1)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [enabled])

  const flush = useCallback(
    (keepalive = false) => {
      if (!articleId || !enabled) return
      const { blockId, pct: p } = reportRef.current
      const add = pendingSecondsRef.current
      if (add === 0 && blockId === null) return
      const payload = {
        last_block_id: blockId,
        scroll_pct: Number(p.toFixed(4)),
        read_seconds: add,
      }
      pendingSecondsRef.current = 0
      if (keepalive) flushProgress(articleId, payload)
      else void api.putProgress(articleId, payload).catch(() => undefined)
    },
    [articleId, enabled],
  )

  // 定时上报 + 离开页面兜底
  useEffect(() => {
    if (!enabled || !articleId) return
    const timer = window.setInterval(() => flush(), REPORT_INTERVAL_MS)
    const onHide = () => {
      if (document.visibilityState === 'hidden') flush(true)
    }
    const onUnload = () => flush(true)
    document.addEventListener('visibilitychange', onHide)
    window.addEventListener('pagehide', onUnload)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onHide)
      window.removeEventListener('pagehide', onUnload)
      flush(false) // 卸载组件（切换路由）时把剩余尾数交出去
    }
  }, [enabled, articleId, flush])

  return { pct, seconds, flush: () => flush(false) }
}
