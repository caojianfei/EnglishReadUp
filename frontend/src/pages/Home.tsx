/** 首页：提交 URL、看抓取进度、进文章。 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'

import { api, articleExportUrl, downloadExport, dropCacheFor } from '../lib/api'
import { formatDate, formatDuration, formatWords } from '../lib/prefs'
import type { Article } from '../lib/types'
import AppNav from '../components/AppNav'

const SAMPLES = [
  'https://www.sixthtone.com/news/1018994',
  'https://www.chinadaily.com.cn/a/202609/11/WS6aa35187e4b06d4aa055d756.html',
]

function StatusBadge({ a }: { a: Article }) {
  if (a.status === 'fetching' || a.status === 'pending') {
    return (
      <span className="er-tag animate-pulse !bg-amber-100 !text-amber-700">
        {a.status === 'pending' ? '排队中' : '抓取中'}
      </span>
    )
  }
  if (a.status === 'failed') {
    return <span className="er-tag !bg-red-100 !text-red-700">抓取失败</span>
  }
  return <span className="er-tag !bg-emerald-100 !text-emerald-700">已完成</span>
}

function ArticleRow({ a, onDelete }: { a: Article; onDelete: (a: Article) => void }) {
  const busy = a.status === 'fetching' || a.status === 'pending'
  const pct = Math.round((a.scroll_pct ?? 0) * 100)

  return (
    <div className="er-card flex gap-4 p-3.5">
      <Link
        to={`/a/${a.id}`}
        className="hidden h-[86px] w-[132px] shrink-0 overflow-hidden rounded-md bg-black/5 sm:block"
      >
        {a.cover_url ? (
          <img
            src={a.cover_url}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-[11px] text-neutral-400">
            无封面
          </div>
        )}
      </Link>

      <div className="flex min-w-0 flex-1 flex-col justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-start gap-2">
            <Link
              to={`/a/${a.id}`}
              className="line-clamp-2 flex-1 font-medium leading-snug hover:underline"
              style={{ color: 'var(--ink)' }}
            >
              {a.title || a.url}
            </Link>
            <StatusBadge a={a} />
          </div>
          <div className="er-muted mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs">
            <span>{a.sitename || a.source_domain}</span>
            <span>·</span>
            <span>{formatWords(a.word_count)}</span>
            {a.status === 'done' ? (
              <>
                <span>·</span>
                <span>{a.block_count} 块</span>
                <span>·</span>
                <span>{a.image_count} 图</span>
              </>
            ) : null}
            {a.published_at ? (
              <>
                <span>·</span>
                <span>{formatDate(a.published_at)}</span>
              </>
            ) : null}
          </div>

          {a.status === 'failed' && a.error_msg ? (
            <p className="mt-1.5 line-clamp-2 text-xs text-red-600">{a.error_msg}</p>
          ) : null}

          {a.read_seconds > 0 ? (
            <p className="er-muted mt-1 text-xs">
              已读 {formatDuration(a.read_seconds)}
              {pct > 0 && pct < 100 ? ` · 停在 ${pct}%` : ''}
            </p>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Link
            className="er-btn er-btn-primary no-underline disabled:opacity-40"
            to={`/a/${a.id}`}
            aria-disabled={busy}
          >
            {busy ? '抓取中…' : pct > 1 ? '继续阅读' : '开始阅读'}
          </Link>
          <a className="er-btn no-underline" href={`/api/articles/${a.id}/markdown`}>
            导出 MD
          </a>
          <button
            className="er-btn"
            onClick={() =>
              downloadExport(articleExportUrl(a.id, 'anki'), `article-${a.id}.csv`)
            }
            title="把这篇里划过、且解析过的词导出成 Anki 卡片"
          >
            生词 CSV
          </button>
          {!busy && a.status === 'done' ? (
            <Link className="er-btn no-underline" to={`/a/${a.id}/summary`}>
              清单
            </Link>
          ) : null}
          <button
            className="er-btn"
            onClick={async () => {
              try {
                await api.refetch(a.id)
              } catch {
                /* 409 = 已在抓取，忽略 */
              }
            }}
            disabled={busy}
          >
            重抓
          </button>
          <button className="er-btn er-btn-danger" onClick={() => onDelete(a)}>
            删除
          </button>
          <span className="er-muted ml-auto max-w-[320px] truncate text-[11px]" title={a.url}>
            #{a.id} {a.url}
          </span>
        </div>
      </div>
    </div>
  )
}

export default function Home() {
  const [url, setUrl] = useState('')
  const [force, setForce] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const qc = useQueryClient()
  const navigate = useNavigate()

  const list = useQuery({
    queryKey: ['articles'],
    queryFn: () => api.listArticles(100, 0),
    // 有任务在跑就轮询；全静止时停掉，别白白打接口
    refetchInterval: (q) =>
      q.state.data?.items.some((a) => a.status === 'pending' || a.status === 'fetching')
        ? 2500
        : false,
  })

  const create = useMutation({
    mutationFn: (u: string) => api.createArticle(u, force),
    onSuccess: (a) => {
      setUrl('')
      setMsg({ kind: 'ok', text: `已提交 #${a.id}，后台抓取中…` })
      void qc.invalidateQueries({ queryKey: ['articles'] })
      if (a.status === 'done') navigate(`/a/${a.id}`)
    },
    onError: (e: Error) => setMsg({ kind: 'err', text: e.message }),
  })

  const remove = useMutation({
    mutationFn: async (a: Article) => {
      await api.deleteArticle(a.id)
      dropCacheFor(a.id)
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['articles'] }),
  })

  const onDelete = (a: Article) => {
    if (!window.confirm(`删除《${a.title || a.url}》？\n该文章的标注与 AI 记录会一并删除，不可撤销。`))
      return
    remove.mutate(a)
  }

  return (
    <div style={{ background: 'var(--panel)', minHeight: '100vh' }}>
      <AppNav />
      <div className="mx-auto max-w-[900px] px-5 py-8">
        <h1 className="text-2xl font-semibold tracking-tight" style={{ color: 'var(--ink)' }}>
          EnglishReadUp
        </h1>
      <p className="er-muted mt-1 text-sm">
        粘贴文章链接 → 本地抓取正文与图片 → 精读并留下可检索的痕迹
      </p>

      <form
        className="mt-6"
        onSubmit={(e) => {
          e.preventDefault()
          setMsg(null)
          if (url.trim()) create.mutate(url.trim())
        }}
      >
        <div className="flex gap-2">
          <input
            className="er-input"
            placeholder="https://www.sixthtone.com/news/..."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            spellCheck={false}
          />
          <button className="er-btn er-btn-primary shrink-0 px-5" disabled={create.isPending}>
            {create.isPending ? '提交中…' : '抓取'}
          </button>
        </div>
        <div className="er-muted mt-2 flex flex-wrap items-center gap-3 text-xs">
          <label className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
              className="accent-[var(--link)]"
            />
            强制浏览器渲染（慢，约 40 秒/篇）
          </label>
          <span className="opacity-70">示例：</span>
          {SAMPLES.map((s) => (
            <button
              key={s}
              type="button"
              className="underline decoration-dotted underline-offset-2 hover:opacity-80"
              onClick={() => setUrl(s)}
            >
              {new URL(s).host.replace('www.', '')}
            </button>
          ))}
        </div>
      </form>

      {msg ? (
        <p className={`mt-3 text-sm ${msg.kind === 'ok' ? 'text-emerald-600' : 'text-red-600'}`}>
          {msg.text}
        </p>
      ) : null}

      <div className="mt-8 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold" style={{ color: 'var(--ink)' }}>
          文章库
          {list.data ? <span className="er-muted ml-2 font-normal">共 {list.data.total} 篇</span> : null}
        </h2>
        {list.isFetching ? <span className="er-muted text-xs">刷新中…</span> : null}
      </div>

      <div className="mt-3 flex flex-col gap-3">
        {list.isLoading ? <p className="er-muted text-sm">加载中…</p> : null}
        {list.isError ? (
          <p className="text-sm text-red-600">
            读取失败：{(list.error as Error).message}
          </p>
        ) : null}
        {list.data && list.data.items.length === 0 ? (
          <p className="er-muted text-sm">还没有文章，先在上面粘一个链接。</p>
        ) : null}
        {list.data?.items.map((a) => (
          <ArticleRow key={a.id} a={a} onDelete={onDelete} />
        ))}
      </div>

      <p className="er-muted mt-10 text-xs">
        仅在本机保存，用于个人学习；保留原文链接与来源标注，不做分发。
      </p>
      </div>
    </div>
  )
}
