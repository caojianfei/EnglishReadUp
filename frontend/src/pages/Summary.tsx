/**
 * 单篇汇总清单（M4，需求 6）。
 *
 * 「生词表 + 语法清单」不是重新生成的内容，而是把这篇里每一处划词/选段时
 * 模型已经产出的结果聚合起来（后端 `services/summarize` 只做聚合与去重）。
 * 好处是清单里的每一行都能指回原文，用户看到的是自己真正读过的句子。
 *
 * 这里**没有**「划了线但还没解析」那一栏：划一段句子不落痕，点过【解析】
 * 才有痕迹与解析记录 —— 清单里的东西必定是解析过的，「待解析」不是一种状态。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import AppNav from '../components/AppNav'
import { api, articleExportUrl, downloadExport } from '../lib/api'
import { displayPhonetic, phoneticNote } from '../lib/phonetic'
import type { GrammarPoint } from '../lib/types'

function GrammarCard({ g, articleId }: { g: GrammarPoint; articleId: number }) {
  const qc = useQueryClient()
  const remove = useMutation({
    mutationFn: () => api.deleteGrammar(g.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['summary', articleId] })
      void qc.invalidateQueries({ queryKey: ['grammar'] })
    },
  })

  return (
    <div className="er-card p-3.5" data-testid="grammar-row">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-sm font-semibold" style={{ color: 'var(--ink)' }}>
          {g.pattern || '（未命名结构）'}
        </span>
        {g.category ? <span className="er-tag">{g.category}</span> : null}
      </div>
      <p className="mt-1.5 text-sm leading-relaxed" style={{ color: 'var(--ink-soft)' }}>
        {g.explanation_cn}
      </p>
      {g.source_sentence ? (
        <p
          className="mt-1.5 font-serif text-xs leading-relaxed"
          style={{ color: 'var(--ink-soft)', borderLeft: '2px solid var(--rule)', paddingLeft: 8 }}
        >
          {g.source_sentence}
        </p>
      ) : null}
      <div className="mt-2 flex gap-1.5">
        <button
          className="er-btn er-btn-danger !px-2 !py-1 !text-[11px]"
          onClick={() => remove.mutate()}
        >
          从清单移除
        </button>
      </div>
    </div>
  )
}

export default function SummaryPage() {
  const { id } = useParams()
  const articleId = Number(id)
  const valid = Number.isFinite(articleId) && articleId > 0

  const summary = useQuery({
    queryKey: ['summary', articleId],
    queryFn: () => api.summary(articleId),
    enabled: valid,
  })

  const data = summary.data
  const stats = data?.stats

  return (
    <div style={{ background: 'var(--panel)', minHeight: '100vh' }}>
      <AppNav />
      <div className="mx-auto max-w-[900px] px-5 py-6">
        {summary.isLoading ? <p className="er-muted text-sm">加载中…</p> : null}
        {summary.isError ? (
          <p className="text-sm text-red-600">读取失败：{(summary.error as Error).message}</p>
        ) : null}

        {data ? (
          <>
            <Link to={`/a/${articleId}`} className="er-muted text-xs no-underline hover:underline">
              ← 回到阅读
            </Link>
            <h1
              className="mt-2 text-xl font-semibold leading-snug"
              style={{ color: 'var(--ink)' }}
              data-testid="summary-title"
            >
              {data.article.title || data.article.url}
            </h1>
            <p className="er-muted mt-1 text-xs">
              {[data.article.sitename, (data.article.published_at ?? '').slice(0, 10)]
                .filter(Boolean)
                .join(' · ')}
              {stats
                ? ` · 共 ${stats.words} 词 · ${stats.grammar} 语法点 · 划过 ${stats.highlights} 处`
                : ''}
            </p>

            <div className="mt-4 flex flex-wrap gap-1.5">
              <button
                className="er-btn er-btn-primary"
                data-testid="export-summary-md"
                onClick={() =>
                  downloadExport(articleExportUrl(articleId, 'md'), `article-${articleId}-summary.md`)
                }
              >
                导出清单 MD
              </button>
              <button
                className="er-btn"
                data-testid="export-summary-anki"
                onClick={() =>
                  downloadExport(articleExportUrl(articleId, 'anki'), `article-${articleId}.csv`)
                }
              >
                导出 Anki CSV
              </button>
              <button
                className="er-btn"
                onClick={() =>
                  downloadExport(articleExportUrl(articleId, 'text'), `article-${articleId}.md`)
                }
              >
                导出原文 MD
              </button>
            </div>

            {/* ---- 生词表 ---- */}
            <h2 className="mt-7 text-sm font-semibold" style={{ color: 'var(--ink)' }}>
              生词表
              <span className="er-muted ml-2 font-normal">{stats?.words ?? 0} 词</span>
            </h2>
            <div className="mt-2.5 overflow-hidden rounded-lg border" style={{ borderColor: 'var(--rule)' }}>
              {data.words.length === 0 ? (
                <p className="er-muted p-3.5 text-sm">
                  本篇还没有生词 —— 在正文里划几个词就会自动收录。
                </p>
              ) : (
                <table className="w-full border-collapse text-sm" data-testid="summary-words">
                  <thead>
                    <tr style={{ background: 'var(--code-bg)' }}>
                      {['单词', '音标', '词性', '释义', '文中例句'].map((h) => (
                        <th
                          key={h}
                          className="px-3 py-2 text-left text-xs font-semibold"
                          style={{ color: 'var(--ink-soft)' }}
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.words.map((w) => (
                      <tr key={w.id} className="border-t" style={{ borderColor: 'var(--rule)', background: 'var(--paper)' }}>
                        {/* 单词列 / 音标列都按「一个词条 + 一个符号」整体排版：
                            不允许列内折行，否则窄列会把「重点」拆成竖排两字、
                            把 /ˈmem.ə.ri/ 从中间断开。 */}
                        <td className="whitespace-nowrap px-3 py-2 align-top font-medium">
                          {w.lemma}
                          {w.worth ? <span className="er-chip ml-1.5">重点</span> : null}
                        </td>
                        <td
                          className="whitespace-nowrap px-3 py-2 align-top font-serif"
                          style={{ color: 'var(--link)' }}
                        >
                          {displayPhonetic(w.phonetic) || '——'}
                          {phoneticNote(w.lemma, w.phonetic_of) ? (
                            <span className="er-muted ml-1 text-[10px]">← {w.phonetic_of}</span>
                          ) : null}
                        </td>
                        <td className="px-3 py-2 align-top" style={{ color: 'var(--ink-soft)' }}>
                          {w.pos || '——'}
                        </td>
                        <td className="px-3 py-2 align-top" style={{ color: 'var(--ink-soft)' }}>
                          {w.meaning_cn || '——'}
                          {w.meaning_in_ctx && w.meaning_in_ctx !== w.meaning_cn ? (
                            <span className="er-muted block text-[11px]">此处：{w.meaning_in_ctx}</span>
                          ) : null}
                        </td>
                        <td
                          className="px-3 py-2 align-top font-serif text-xs"
                          style={{ color: 'var(--ink-soft)' }}
                        >
                          {w.sentence || w.example || '——'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* ---- 语法清单 ---- */}
            <h2 className="mt-7 text-sm font-semibold" style={{ color: 'var(--ink)' }}>
              语法清单
              <span className="er-muted ml-2 font-normal">
                {stats?.grammar ?? 0} 条
                {Object.keys(data.grammar_by_category).length
                  ? ` · ${Object.entries(data.grammar_by_category)
                      .map(([k, v]) => `${k} ${v}`)
                      .join(' / ')}`
                  : ''}
              </span>
            </h2>
            <div className="mt-2.5 grid gap-3">
              {data.grammar.length === 0 ? (
                <p className="er-muted text-sm">
                  本篇还没有语法点 —— 选中一个句子点【解析】就会收录。
                </p>
              ) : null}
              {data.grammar.map((g) => (
                <GrammarCard key={g.id} g={g} articleId={articleId} />
              ))}
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
