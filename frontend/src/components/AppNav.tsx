/**
 * 顶部导航。
 *
 * M4 多了生词本与检索两个入口，它们和首页/阅读页是平级的 ——
 * 把导航抽出来放在每个页面顶部，比在每页各写一遍链接可靠。
 *
 * 徽标数字刻意来自两个不同的端点：
 *   生词本 —— `vocab/stats.total`（攒了多少词，是「资产」）
 *   复习   —— `review/stats.due_now`（欠了多少张，是「待办」）
 * 后者为 0 时不显示数字，显示的是「今天没有欠账」，而不是一个 0。
 */

import { useQuery } from '@tanstack/react-query'
import { Link, useLocation } from 'react-router-dom'

import { api } from '../lib/api'

const TABS = [
  { to: '/', label: '文章库', match: (p: string) => p === '/' },
  { to: '/vocab', label: '生词本', match: (p: string) => p.startsWith('/vocab') },
  { to: '/review', label: '复习', match: (p: string) => p.startsWith('/review') },
  { to: '/search', label: '检索', match: (p: string) => p.startsWith('/search') },
  { to: '/pos', label: '词性', match: (p: string) => p.startsWith('/pos') },
]

export default function AppNav() {
  const { pathname } = useLocation()
  const stats = useQuery({
    queryKey: ['vocab-stats'],
    queryFn: api.vocabStats,
    staleTime: 20_000,
  })
  const review = useQuery({
    queryKey: ['review-stats'],
    queryFn: api.reviewStats,
    staleTime: 20_000,
  })

  const badge = (to: string): number | null => {
    if (to === '/vocab') return stats.data?.total ?? null
    if (to === '/review') return review.data?.due_now ?? null
    return null
  }

  return (
    <header className="er-toolbar sticky top-0 z-30">
      <nav className="mx-auto flex max-w-[1100px] items-center gap-1 overflow-x-auto px-4 py-2">
        <Link
          to="/"
          className="mr-3 shrink-0 whitespace-nowrap text-sm font-semibold no-underline"
          style={{ color: 'var(--ink)' }}
        >
          精读
        </Link>
        {TABS.map((t) => {
          const on = t.match(pathname)
          const n = badge(t.to)
          return (
            <Link
              key={t.to}
              to={t.to}
              data-on={on ? '1' : undefined}
              data-testid={`nav-${t.label}`}
              className="shrink-0 whitespace-nowrap rounded-md px-2.5 py-1 text-xs no-underline transition-colors"
              style={{ color: on ? 'var(--link)' : 'var(--ink-soft)', fontWeight: on ? 600 : 400 }}
            >
              {t.label}
              {n ? <span className="er-muted ml-1 text-[10px]">{n}</span> : null}
            </Link>
          )
        })}
      </nav>
    </header>
  )
}
