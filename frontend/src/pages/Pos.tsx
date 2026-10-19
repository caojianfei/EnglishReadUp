/**
 * 词性速查（只读参考页）。
 *
 * 这页不联网、不写库：清单是 `lib/pos.ts` 里的静态数据，打开就能查。
 * 三个细节是刻意的：
 *   1. 简称口径与词卡一致（adj. / vt. 而不是 a.），清单才能和查词结果对上；
 *   2. 搜索覆盖简称、全称、中文名、解释和例句 —— 忘了简称的人往往只记得「副词」；
 *   3. 句子成分单独一组并明确标注「不是词性」，S / O / C 是最常被误认的一类。
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import AppNav from '../components/AppNav'
import { POS_GROUPS, POS_IN_APP, POS_TOTAL } from '../lib/pos'
import type { SearchablePosEntry } from '../lib/pos'

/** sticky 分组条的高度：分组锚点滚动时要把它让出来 */
const ANCHOR_OFFSET = 136

function AbbrBadge({ abbr, small }: { abbr: string; small?: boolean }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center rounded-md border font-mono font-semibold ${
        small ? 'min-w-[2.5rem] px-1.5 py-0.5 text-[11px]' : 'mt-0.5 h-7 min-w-[3.25rem] px-2 text-[13px]'
      }`}
      style={{
        background: 'color-mix(in srgb, var(--link) 10%, transparent)',
        borderColor: 'color-mix(in srgb, var(--link) 35%, transparent)',
        color: 'var(--link)',
      }}
    >
      {abbr}
    </span>
  )
}

function Example({ entry }: { entry: SearchablePosEntry }) {
  return (
    <div className="mt-2.5 border-l-2 pl-2.5" style={{ borderColor: 'var(--rule)' }}>
      <p className="font-serif text-[13px] leading-relaxed" style={{ color: 'var(--ink)' }}>
        {entry.example.en}
      </p>
      <p className="mt-0.5 text-[12px] leading-relaxed" style={{ color: 'var(--ink-faint)' }}>
        {entry.example.zh}
      </p>
    </div>
  )
}

function PosCard({ entry, groupKey }: { entry: SearchablePosEntry; groupKey: string }) {
  return (
    <article className="er-card p-3.5" data-testid={`pos-entry-${groupKey}-${entry.abbr}`}>
      <div className="flex items-start gap-2.5">
        <AbbrBadge abbr={entry.abbr} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <h3 className="text-sm font-semibold" style={{ color: 'var(--ink)' }}>
              {entry.nameZh}
            </h3>
            <span className="er-muted text-xs">{entry.nameEn}</span>
            {entry.aliases?.length ? (
              <span className="er-muted text-[11px]">也作 {entry.aliases.join(' / ')}</span>
            ) : null}
            {entry.inApp ? <span className="er-tag">本站</span> : null}
          </div>
          <p className="mt-1.5 text-[13px] leading-relaxed" style={{ color: 'var(--ink-soft)' }}>
            {entry.summary}
          </p>
          <p className="mt-1 text-[12px] leading-relaxed" style={{ color: 'var(--ink-faint)' }}>
            用法：{entry.usage}
          </p>
          <Example entry={entry} />
        </div>
      </div>
    </article>
  )
}

function SyntaxTable({ entries }: { entries: SearchablePosEntry[] }) {
  return (
    <div className="er-card mt-3 overflow-x-auto">
      <table className="w-full min-w-[620px] border-collapse text-left text-[13px]">
        <thead>
          <tr className="er-muted text-[11px] uppercase tracking-wide">
            <th className="px-3 py-2 font-medium">缩写</th>
            <th className="px-3 py-2 font-medium">成分</th>
            <th className="px-3 py-2 font-medium">说明</th>
            <th className="px-3 py-2 font-medium">例句</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr
              key={entry.abbr}
              className="border-t align-top"
              style={{ borderColor: 'var(--rule)' }}
              data-testid={`pos-entry-syntax-${entry.abbr}`}
            >
              <td className="px-3 py-3">
                <AbbrBadge abbr={entry.abbr} small />
              </td>
              <td className="px-3 py-3">
                <div className="font-medium" style={{ color: 'var(--ink)' }}>
                  {entry.nameZh}
                </div>
                <div className="er-muted mt-0.5 text-[11px]">{entry.nameEn}</div>
              </td>
              <td className="px-3 py-3" style={{ color: 'var(--ink-soft)' }}>
                {entry.summary}
                <div className="mt-1 text-[12px]" style={{ color: 'var(--ink-faint)' }}>
                  用法：{entry.usage}
                </div>
              </td>
              <td className="max-w-[280px] px-3 py-3">
                <p className="font-serif leading-relaxed" style={{ color: 'var(--ink)' }}>
                  {entry.example.en}
                </p>
                <p className="mt-0.5 text-[12px]" style={{ color: 'var(--ink-faint)' }}>
                  {entry.example.zh}
                </p>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Pos() {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(POS_GROUPS[0].key)
  const inputRef = useRef<HTMLInputElement>(null)

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return POS_GROUPS
    return POS_GROUPS.map((g) => ({
      ...g,
      entries: g.entries.filter((e) => e.search.includes(q)),
    })).filter((g) => g.entries.length > 0)
  }, [query])

  const hits = useMemo(() => groups.reduce((n, g) => n + g.entries.length, 0), [groups])

  // 滚动时点亮对应的分组胶囊；搜索把某组过滤掉后，不留下指向空组的选中态
  useEffect(() => {
    const onScroll = () => {
      if (groups.length === 0) return
      let current = groups[0].key
      // 多给 8px 容差：scroll-mt 落点可能是 136.4 这种小数，严格比较会选到上一组
      for (const g of groups) {
        const el = document.getElementById(`pos-${g.key}`)
        if (el && el.getBoundingClientRect().top <= ANCHOR_OFFSET + 8) current = g.key
      }
      // 滚到底时最后一组未必能顶到锚点高度，按「已到页底」兜底点亮它
      const atBottom =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4
      if (atBottom) current = groups[groups.length - 1].key
      setActive(current)
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [groups])

  // `/` 聚焦搜索：这是「随时查」的页面，不该先找鼠标
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      const typing =
        t instanceof HTMLInputElement ||
        t instanceof HTMLTextAreaElement ||
        t instanceof HTMLSelectElement ||
        !!t?.isContentEditable
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        inputRef.current?.focus()
        inputRef.current?.select()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const jump = (key: string) => {
    setActive(key)
    const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const scroll = () =>
      document.getElementById(`pos-${key}`)?.scrollIntoView({
        behavior: smooth ? 'smooth' : 'auto',
        block: 'start',
      })
    if (query) {
      setQuery('')
      // 等过滤结果重新渲染后再滚，否则目标分组可能还没回到 DOM 里
      window.setTimeout(scroll, 0)
    } else {
      scroll()
    }
  }

  return (
    <div style={{ background: 'var(--panel)', minHeight: '100vh' }} data-testid="pos-page">
      <AppNav />
      <main className="mx-auto max-w-[1100px] px-5 pb-20 pt-7">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p
              className="text-[11px] font-semibold uppercase tracking-[0.18em]"
              style={{ color: 'var(--link)' }}
            >
              Reference
            </p>
            <h1
              className="mt-1 text-[28px] font-semibold leading-tight tracking-tight"
              style={{ color: 'var(--ink)' }}
            >
              词性速查
            </h1>
            <p className="mt-1.5 max-w-[560px] text-[13px] leading-relaxed" style={{ color: 'var(--ink-soft)' }}>
              简称、英文全称、释义与例句。简称口径与本站词卡一致 —— 查词时看到的 adj.、vt. 就是这里的写法。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-[11px]" style={{ color: 'var(--ink-soft)' }}>
            <span className="er-tag">共 {POS_TOTAL} 条</span>
            <span className="er-tag">本站词卡常见 {POS_IN_APP} 条</span>
            <span className="er-muted hidden sm:inline">
              按 <kbd className="rounded border px-1 font-mono">/</kbd> 搜索
            </span>
          </div>
        </header>

        {/* sticky 检索条：翻到分组深处也能直接改关键词 */}
        <div
          className="sticky top-[41px] z-20 -mx-5 mt-5 border-b px-5 py-2.5"
          style={{ background: 'var(--panel)', borderColor: 'var(--rule)' }}
        >
          <div className="flex flex-col gap-2 md:flex-row md:items-center">
            <div className="relative shrink-0 md:w-[300px]">
              <svg
                aria-hidden="true"
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2"
                style={{ color: 'var(--ink-faint)' }}
              >
                <circle cx="8.5" cy="8.5" r="5.5" />
                <path d="M12.5 12.5 17 17" strokeLinecap="round" />
              </svg>
              <label htmlFor="pos-search" className="sr-only">
                搜索词性
              </label>
              <input
                id="pos-search"
                ref={inputRef}
                type="search"
                value={query}
                placeholder="搜简称、中文名或例句…"
                autoComplete="off"
                data-testid="pos-search"
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.preventDefault()
                    setQuery('')
                  }
                }}
                className="er-input !py-2 !pl-9 !pr-8 !text-[13px] [&::-webkit-search-cancel-button]:hidden"
              />
              {query ? (
                <button
                  type="button"
                  aria-label="清空搜索"
                  onClick={() => {
                    setQuery('')
                    inputRef.current?.focus()
                  }}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-1 text-sm leading-none"
                  style={{ color: 'var(--ink-faint)' }}
                >
                  ×
                </button>
              ) : null}
            </div>

            <div className="flex gap-1.5 overflow-x-auto pb-0.5">
              {POS_GROUPS.map((g) => (
                <button
                  key={g.key}
                  type="button"
                  className="er-btn shrink-0"
                  data-on={active === g.key ? '1' : undefined}
                  aria-pressed={active === g.key}
                  data-testid={`pos-jump-${g.key}`}
                  onClick={() => jump(g.key)}
                >
                  {g.title}
                </button>
              ))}
            </div>

            <div
              className="er-muted shrink-0 text-[11px] md:ml-auto"
              aria-live="polite"
              data-testid="pos-count"
            >
              {query.trim() ? `命中 ${hits} 条` : `共 ${POS_TOTAL} 条`}
            </div>
          </div>
        </div>

        {groups.length === 0 ? (
          <div className="er-card mt-8 p-10 text-center" data-testid="pos-empty">
            <p className="text-sm font-semibold" style={{ color: 'var(--ink)' }}>
              没有匹配「{query.trim()}」的词性
            </p>
            <p className="er-muted mt-1 text-xs">
              试试 adj、副词、preposition，或者例句里出现过的词。
            </p>
            <button
              type="button"
              className="er-btn er-btn-primary mt-4"
              onClick={() => {
                setQuery('')
                inputRef.current?.focus()
              }}
            >
              清空搜索
            </button>
          </div>
        ) : null}

        {groups.map((g) => (
          <section
            key={g.key}
            id={`pos-${g.key}`}
            className="scroll-mt-[136px] pt-7"
            data-testid={`pos-group-${g.key}`}
          >
            <div
              className="flex items-start justify-between gap-3 border-b pb-2"
              style={{ borderColor: 'var(--rule)' }}
            >
              <div>
                <h2 className="flex flex-wrap items-baseline gap-x-2 text-base font-semibold" style={{ color: 'var(--ink)' }}>
                  {g.title}
                  <span className="er-muted text-[11px] font-normal uppercase tracking-wide">
                    {g.subtitle}
                  </span>
                </h2>
                <p className="mt-1 max-w-[720px] text-xs leading-relaxed" style={{ color: 'var(--ink-soft)' }}>
                  {g.intro}
                </p>
              </div>
              <span className="er-muted shrink-0 text-[11px]">{g.entries.length} 条</span>
            </div>

            {g.key === 'syntax' ? (
              <SyntaxTable entries={g.entries} />
            ) : (
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                {g.entries.map((e) => (
                  <PosCard key={e.abbr} entry={e} groupKey={g.key} />
                ))}
              </div>
            )}
          </section>
        ))}

        <p
          className="mt-10 border-t pt-4 text-[11px] leading-relaxed"
          style={{ borderColor: 'var(--rule)', color: 'var(--ink-faint)' }}
        >
          简称来自本地词典 ECDICT 的词性标注；带「本站」标签的
          {' '}
          {POS_IN_APP}
          {' '}
          条会直接出现在划词词卡和生词本里。清单只作参考，具体词义仍以词卡释义为准。
        </p>
      </main>
    </div>
  )
}
