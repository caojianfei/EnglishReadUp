/**
 * 生词本（M4）。
 *
 * 这一页只做「看与改」—— 收录是划词时自动发生的（后端 `services/vocab.enroll`），
 * 没有「加入生词本」这个按钮。需求 5 的立场是不漏，靠手动收藏必然漏。
 *
 * 筛选维度对应三种真实用法：
 *   按状态   —— 复习时把「已掌握」的滤掉
 *   只看重点 —— 词攒到几百条之后，先啃模型判定值得记的（worth=1）
 *   按文章   —— 刚精读完一篇，把这批词过一遍
 */

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import AccentPhonetics from '../components/AccentPhonetics'
import AppNav from '../components/AppNav'
import { api, downloadExport, dropDictCache, vocabExportUrl } from '../lib/api'
import { phoneticNote } from '../lib/phonetic'
import type { Vocab, VocabStatus } from '../lib/types'
import { VOCAB_STATUS_LABEL } from '../lib/types'

const STATUS_TABS: { key: '' | VocabStatus; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'new', label: '生词' },
  { key: 'learning', label: '在学' },
  { key: 'known', label: '已掌握' },
]

const SORTS = [
  { key: 'priority', label: '按重要度' },
  { key: 'recent', label: '最新收录' },
  { key: 'seen', label: '划过最多' },
  { key: 'alpha', label: '字母序' },
] as const

function WordRow({ v, articleId }: { v: Vocab; articleId: number | null }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(v.meaning_cn ?? '')

  const patch = useMutation({
    mutationFn: (payload: { status?: string; meaning_cn?: string; worth?: number }) =>
      api.patchVocab(v.id, payload),
    onSuccess: (_data, payload) => {
      // 改释义是全局修正（词卡里也生效），断网兜底的查词缓存得一起清掉
      if (payload.meaning_cn !== undefined) dropDictCache(v.lemma)
      void qc.invalidateQueries({ queryKey: ['vocab'] })
      void qc.invalidateQueries({ queryKey: ['vocab-stats'] })
      setEditing(false)
    },
  })

  const remove = useMutation({
    mutationFn: () => api.deleteVocab(v.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['vocab'] })
      void qc.invalidateQueries({ queryKey: ['vocab-stats'] })
    },
  })

  const mine = useMemo(
    // 单条详情里带 sources；列表接口则已经按当前文章筛好了 sentence
    () => (articleId != null ? v.sources?.find((s) => s.article_id === articleId) : undefined),
    [v.sources, articleId],
  )
  const sentence = mine?.sentence ?? v.sentence

  return (
    <div className="er-card p-3.5" data-testid="vocab-row" data-lemma={v.lemma}>
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <span className="text-base font-semibold" data-testid="vocab-lemma">
          {v.lemma}
        </span>
        {/* 英美两条都摆出来（M12 之前只显示主音标）：点「英」那个喇叭听到的读音，
            在卡上得能找到对应的一条音标。缺哪边只显示哪边 —— 见 AccentPhonetics。 */}
        <AccentPhonetics
          uk={v.phonetic_uk}
          us={v.phonetic_us}
          audios={v.audios}
          word={v.lemma}
          compact
          className="text-sm"
          phoneticStyle={{ color: 'var(--link)' }}
          emptyText="无音标"
          testId="vocab-phonetic"
        />
        {v.pos ? <span className="text-xs" style={{ color: 'var(--ink-soft)' }}>{v.pos}</span> : null}
        {phoneticNote(v.lemma, v.phonetic_of) ? (
          <span className="er-muted text-[11px]">{phoneticNote(v.lemma, v.phonetic_of)}</span>
        ) : null}
        {v.worth ? <span className="er-chip">重点</span> : null}
        {v.seen_count > 1 ? (
          <span className="er-muted text-[11px]">划过 {v.seen_count} 次</span>
        ) : null}
      </div>

      {editing ? (
        <div className="mt-2 flex gap-1.5">
          <input
            className="er-input !py-1.5 !text-sm"
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') patch.mutate({ meaning_cn: draft })
              if (e.key === 'Escape') setEditing(false)
            }}
          />
          <button className="er-btn er-btn-primary" onClick={() => patch.mutate({ meaning_cn: draft })}>
            保存
          </button>
          <button className="er-btn" onClick={() => setEditing(false)}>
            取消
          </button>
        </div>
      ) : (
        <p className="mt-1.5 text-sm" data-testid="vocab-meaning">
          {v.meaning_cn || '（无释义）'}
          {v.meaning_edited ? (
            <span className="er-muted ml-1.5 text-[11px]" data-testid="vocab-meaning-edited">
              （已修正）
            </span>
          ) : null}
        </p>
      )}

      {v.meaning_in_ctx && v.meaning_in_ctx !== v.meaning_cn ? (
        <p className="mt-1 text-xs leading-relaxed" style={{ color: 'var(--ink-soft)' }}>
          此处：{v.meaning_in_ctx}
        </p>
      ) : null}

      {sentence ? (
        <p
          className="mt-1.5 font-serif text-xs leading-relaxed"
          style={{ color: 'var(--ink-soft)', borderLeft: '2px solid var(--rule)', paddingLeft: 8 }}
        >
          {sentence}
        </p>
      ) : null}

      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
        {(Object.keys(VOCAB_STATUS_LABEL) as VocabStatus[]).map((s) => (
          <button
            key={s}
            className="er-btn !px-2 !py-1 !text-[11px]"
            data-on={v.status === s ? '1' : undefined}
            data-testid={`status-${s}`}
            onClick={() => patch.mutate({ status: s })}
          >
            {VOCAB_STATUS_LABEL[s]}
          </button>
        ))}
        <button
          className="er-btn !px-2 !py-1 !text-[11px]"
          onClick={() => {
            setDraft(v.meaning_cn ?? '')
            setEditing(true)
          }}
        >
          改释义
        </button>
        {/* 清空释义 = 删掉修正、退回词典原文（后端 update_vocab） */}
        {v.meaning_edited ? (
          <button
            className="er-btn !px-2 !py-1 !text-[11px]"
            title="删掉你的修正，退回离线词典的原文（词卡里也一起恢复）"
            data-testid="vocab-meaning-reset"
            onClick={() => patch.mutate({ meaning_cn: '' })}
          >
            恢复词典
          </button>
        ) : null}
        <button
          className="er-btn !px-2 !py-1 !text-[11px]"
          title="切换「值得记」标记，只影响排序与筛选，不会删掉这个词"
          onClick={() => patch.mutate({ worth: v.worth ? 0 : 1 })}
        >
          {v.worth ? '取消重点' : '标为重点'}
        </button>
        <button
          className="er-btn er-btn-danger !px-2 !py-1 !text-[11px]"
          onClick={() => remove.mutate()}
        >
          移除
        </button>
        {v.article_id != null ? (
          <Link
            to={`/a/${v.article_id}`}
            className="er-muted ml-auto text-[11px] no-underline hover:underline"
            title={v.article_titles ?? ''}
          >
            {v.article_titles ? truncate(v.article_titles, 40) : `文章 #${v.article_id}`} ↗
          </Link>
        ) : null}
      </div>
    </div>
  )
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s
}

export default function VocabPage() {
  // 从检索结果点「词形」会带 ?q=xxx 过来，直接落到那个词上
  const [params] = useSearchParams()
  const initialQ = params.get('q') ?? ''

  const [status, setStatus] = useState<'' | VocabStatus>('')
  const [onlyWorth, setOnlyWorth] = useState(false)
  const [articleId, setArticleId] = useState<number | null>(null)
  const [sort, setSort] = useState<'recent' | 'alpha' | 'priority' | 'seen'>('priority')
  const [rawQ, setRawQ] = useState(initialQ)
  const [q, setQ] = useState(initialQ)

  // 输入停顿 250ms 再发请求，否则每敲一个字母打一次接口
  useEffect(() => {
    const t = window.setTimeout(() => setQ(rawQ.trim()), 250)
    return () => window.clearTimeout(t)
  }, [rawQ])

  const articles = useQuery({ queryKey: ['articles'], queryFn: () => api.listArticles(100, 0) })
  const stats = useQuery({ queryKey: ['vocab-stats'], queryFn: api.vocabStats })

  const list = useQuery({
    queryKey: ['vocab', status, onlyWorth, articleId, sort, q],
    queryFn: () =>
      api.listVocab({
        status: status || undefined,
        worth: onlyWorth ? 1 : undefined,
        article_id: articleId,
        sort,
        q: q || undefined,
        limit: 300,
      }),
  })

  const items = list.data?.items ?? []

  return (
    <div style={{ background: 'var(--panel)', minHeight: '100vh' }}>
      <AppNav />
      <div className="mx-auto max-w-[1100px] px-5 py-6">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h1 className="text-xl font-semibold" style={{ color: 'var(--ink)' }}>
            生词本
            {stats.data ? (
              <span className="er-muted ml-2 text-sm font-normal">
                共 {stats.data.total} 词 · 重点 {stats.data.worth} · 今日新增 {stats.data.today}
              </span>
            ) : null}
          </h1>
          <div className="flex gap-1.5">
            <button
              className="er-btn"
              data-testid="export-anki"
              onClick={() => {
                downloadExport(vocabExportUrl({ format: 'anki' }), 'vocab.csv')
                // 触发一次下载即可；文件名由 Content-Disposition 决定
              }}
            >
              导出 Anki CSV
            </button>
            <button
              className="er-btn"
              onClick={() => downloadExport(vocabExportUrl({ format: 'md' }), 'vocab.md')}
            >
              导出 Markdown
            </button>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input
            className="er-input !w-[220px] !py-1.5 !text-sm"
            placeholder="搜词形或释义…"
            value={rawQ}
            onChange={(e) => setRawQ(e.target.value)}
            data-testid="vocab-search"
          />
          <div className="flex gap-1">
            {STATUS_TABS.map((t) => (
              <button
                key={t.key || 'all'}
                className="er-btn"
                data-on={status === t.key ? '1' : undefined}
                onClick={() => setStatus(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <button
            className="er-btn"
            data-on={onlyWorth ? '1' : undefined}
            data-testid="only-worth"
            onClick={() => setOnlyWorth((v) => !v)}
          >
            只看重点
          </button>
          <select
            className="er-input !w-auto !py-1.5 !text-sm"
            value={sort}
            onChange={(e) => setSort(e.target.value as typeof sort)}
            data-testid="vocab-sort"
          >
            {SORTS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
          <select
            className="er-input !w-auto !py-1.5 !text-sm"
            value={articleId ?? ''}
            onChange={(e) => setArticleId(e.target.value ? Number(e.target.value) : null)}
            data-testid="vocab-article"
          >
            <option value="">所有文章</option>
            {articles.data?.items.map((a) => (
              <option key={a.id} value={a.id}>
                {truncate(a.title || a.url, 34)}
              </option>
            ))}
          </select>
          {list.isFetching ? <span className="er-muted text-xs">查询中…</span> : null}
        </div>

        <p className="er-muted mt-3 text-xs" data-testid="vocab-total">
          筛出 {list.data?.total ?? 0} 词
        </p>

        <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {list.isLoading ? <p className="er-muted text-sm">加载中…</p> : null}
          {list.isError ? (
            <p className="text-sm text-red-600">读取失败：{(list.error as Error).message}</p>
          ) : null}
          {!list.isLoading && items.length === 0 ? (
            <p className="er-muted col-span-full text-sm">
              这个条件下还没有词。去阅读页划过线就会自动收录。
            </p>
          ) : null}
          {items.map((v) => (
            <WordRow key={v.id} v={v} articleId={articleId} />
          ))}
        </div>
      </div>
    </div>
  )
}
