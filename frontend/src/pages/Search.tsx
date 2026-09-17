/**
 * 全文检索（M4）。
 *
 * 三组结果，一次输入全给：
 *   正文 —— 「我记得那句话在哪儿」
 *   生词 —— 「我记过这个词，当时是什么意思」
 *   AI   —— 需求 5 要的「AI 交互记录可检索」
 *
 * 每条结果都带跳回原文的位置（article_id + block_id + 区间），点开就能
 * 回到现场并闪一下。没有这一层的搜索结果只是一份文本列表。
 */

import { useCallback, useEffect, useState } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import AppNav from '../components/AppNav'
import { AI_LABEL } from '../components/AiResultView'
import { api } from '../lib/api'
import { displayPhonetic } from '../lib/phonetic'
import type { AiHit, BlockHit, SearchGrouped, VocabHit } from '../lib/types'

/**
 * 片段里只允许 `<mark>` 这一种标签。
 *
 * 片段来自 FTS5 的 `snippet()`，内容是**抓来的网页正文**和**模型输出**，
 * 直接 `dangerouslySetInnerHTML` 等于把第三方内容当 HTML 执行。
 * 做法是先整段转义、再把转义后的 `&lt;mark&gt;` 还原成真标签 ——
 * 白名单只有一个标签，其他任何符号都留在转义态。
 */
function safeSnippet(s: string | null | undefined): string {
  if (!s) return ''
  const esc = s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return esc.replace(/&lt;mark&gt;/g, '<mark>').replace(/&lt;\/mark&gt;/g, '</mark>')
}

function readerLink(
  articleId: number,
  blockId?: number | null,
  start?: number | null,
  end?: number | null,
): string {
  if (blockId == null) return `/a/${articleId}`
  const qs = new URLSearchParams({ block: String(blockId) })
  if (start != null) qs.set('start', String(start))
  if (end != null) qs.set('end', String(end))
  return `/a/${articleId}?${qs.toString()}`
}

function GroupHead({ title, n, hint }: { title: string; n: number; hint: string }) {
  return (
    <h2 className="mt-6 flex items-baseline gap-2 text-sm font-semibold" style={{ color: 'var(--ink)' }}>
      {title}
      <span className="er-muted text-xs font-normal">
        {n} 条 · {hint}
      </span>
    </h2>
  )
}

function BlockRow({ h }: { h: BlockHit }) {
  return (
    <Link
      to={readerLink(h.article_id, h.block_id, h.start, h.end)}
      className="er-card block p-3 no-underline"
      data-testid="hit-block"
    >
      <div className="er-muted mb-1 truncate text-[11px]">{h.article_title}</div>
      <p
        className="er-snip font-serif text-sm leading-relaxed"
        style={{ color: 'var(--ink-soft)' }}
        dangerouslySetInnerHTML={{ __html: safeSnippet(h.snippet) }}
      />
      <span className="er-muted mt-1 inline-block text-[11px]">
        跳到原文 {h.start != null ? `（第 ${h.start} 字起）` : ''} ↗
      </span>
    </Link>
  )
}

function VocabRow({ h }: { h: VocabHit }) {
  return (
    <div className="er-card p-3" data-testid="hit-vocab">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <Link
          to={`/vocab?q=${encodeURIComponent(h.lemma)}`}
          className="text-sm font-semibold no-underline hover:underline"
          style={{ color: 'var(--ink)' }}
        >
          {h.lemma}
        </Link>
        {h.phonetic ? (
          <span className="font-serif text-xs" style={{ color: 'var(--link)' }}>
            {displayPhonetic(h.phonetic)}
          </span>
        ) : null}
        {h.pos ? <span className="er-muted text-[11px]">{h.pos}</span> : null}
        {h.seen_count > 1 ? (
          <span className="er-muted text-[11px]">划过 {h.seen_count} 次</span>
        ) : null}
      </div>
      <p className="mt-1 text-sm" style={{ color: 'var(--ink-soft)' }}>
        {h.meaning_cn}
        {h.meaning_in_ctx && h.meaning_in_ctx !== h.meaning_cn ? `（此处：${h.meaning_in_ctx}）` : ''}
      </p>
      {h.sentence ? (
        <p className="er-muted mt-1 font-serif text-xs leading-relaxed">{h.sentence}</p>
      ) : null}
      {h.article_titles ? (
        <p className="er-muted mt-1 truncate text-[11px]">出自：{h.article_titles}</p>
      ) : null}
    </div>
  )
}

function AiRow({ h, onDelete }: { h: AiHit; onDelete: (id: number) => void }) {
  const body = (
    <>
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <span className="er-tag">{AI_LABEL[h.prompt_type] ?? h.prompt_type}</span>
        <span className="er-muted truncate text-[11px]">{h.article_title}</span>
        {/* mr-5 给右上角的删除按钮让位（它悬停才出现，静止时也不该压着时间戳） */}
        <span className="er-muted ml-auto mr-5 text-[11px]">{h.created_at.slice(0, 16).replace('T', ' ')}</span>
      </div>
      <p
        className="er-snip text-xs leading-relaxed"
        style={{ color: 'var(--ink-soft)' }}
        dangerouslySetInnerHTML={{ __html: safeSnippet(h.snippet) }}
      />
      <span className="er-muted mt-1 inline-block text-[11px]">
        {h.block_id != null ? '跳到原文 ↗' : '痕迹已删，只能回到文章 ↗'}
      </span>
    </>
  )
  return (
    <div className="er-del-host" data-log-id={h.id}>
      <Link
        to={readerLink(h.article_id, h.block_id, h.start, h.end)}
        className="er-card block p-3 no-underline"
        data-testid="hit-ai"
      >
        {body}
      </Link>
      {/* 搜到一条误操作的记录，当然也要能就地删掉 —— 与右栏列表是同一个接口 */}
      <button
        type="button"
        className="er-log-del er-log-del-paper"
        title="删除这条解析记录"
        aria-label="删除这条解析记录"
        data-testid="ai-log-delete"
        onClick={() => onDelete(h.id)}
      >
        ✕
      </button>
    </div>
  )
}

export default function SearchPage() {
  const [raw, setRaw] = useState('')
  const [q, setQ] = useState('')
  const qc = useQueryClient()

  useEffect(() => {
    const t = window.setTimeout(() => setQ(raw.trim()), 300)
    return () => window.clearTimeout(t)
  }, [raw])

  const res = useQuery({
    queryKey: ['search', q],
    queryFn: () => api.search(q, 20),
    enabled: q.length >= 2,
    placeholderData: keepPreviousData,
  })

  /**
   * 删掉一条解析记录（划词误操作的清理口）。
   *
   * 这里的数据是「本次检索的快照」，同样本地先摘掉（含 total 计数），
   * 失败再让 react-query 重新拉一次把它放回来。
   */
  const removeAi = useCallback(
    async (logId: number) => {
      qc.setQueryData<SearchGrouped>(['search', q], (old) =>
        old
          ? {
              ...old,
              total: Math.max(0, old.total - 1),
              ai: old.ai.filter((x) => x.id !== logId),
            }
          : old,
      )
      try {
        await api.deleteAiLog(logId)
      } catch {
        void qc.invalidateQueries({ queryKey: ['search'] })
      }
    },
    [qc, q],
  )

  const d = res.data
  const empty = d && d.total === 0 && !res.isFetching

  return (
    <div style={{ background: 'var(--panel)', minHeight: '100vh' }}>
      <AppNav />
      <div className="mx-auto max-w-[900px] px-5 py-6">
        <h1 className="text-xl font-semibold" style={{ color: 'var(--ink)' }}>
          检索
        </h1>
        <p className="er-muted mt-1 text-sm">
          正文原文、生词本释义、以及每一次 AI 解析的输入与结果 —— 三处一起搜。
        </p>

        <input
          className="er-input mt-4"
          placeholder="输入单词、短语或中文（至少 2 个字符）…"
          value={raw}
          autoFocus
          spellCheck={false}
          data-testid="search-input"
          onChange={(e) => setRaw(e.target.value)}
        />

        {q.length < 2 ? (
          <p className="er-muted mt-4 text-sm">至少输入 2 个字符。</p>
        ) : null}
        {res.isFetching ? <p className="er-muted mt-4 text-sm">检索中…</p> : null}
        {res.isError ? (
          <p className="mt-4 text-sm text-red-600">检索失败：{(res.error as Error).message}</p>
        ) : null}
        {empty ? <p className="er-muted mt-4 text-sm">没有命中「{q}」。</p> : null}

        {d && d.blocks.length ? (
          <>
            <GroupHead title="正文" n={d.blocks.length} hint="点开回到原句" />
            <div className="mt-2 grid gap-2">
              {d.blocks.map((h) => (
                <BlockRow key={`${h.article_id}-${h.block_id}`} h={h} />
              ))}
            </div>
          </>
        ) : null}

        {d && d.vocab.length ? (
          <>
            <GroupHead title="生词本" n={d.vocab.length} hint="点词形进生词本" />
            <div className="mt-2 grid gap-2">
              {d.vocab.map((h) => (
                <VocabRow key={h.id} h={h} />
              ))}
            </div>
          </>
        ) : null}

        {d && d.ai.length ? (
          <>
            <GroupHead title="AI 解析记录" n={d.ai.length} hint="需求 5：每一次解析都可检索" />
            <div className="mt-2 grid gap-2">
              {d.ai.map((h) => (
                <AiRow key={h.id} h={h} onDelete={removeAi} />
              ))}
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
