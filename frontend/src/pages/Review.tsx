/**
 * 间隔复习（M5，需求 6 的延伸）。
 *
 * 一句话设计立场：**这一页只做「考自己」，不做「管理生词」。**
 * 增删改都在生词本页；这里只有一张卡、四个按钮。
 *
 * 三个刻意的取舍：
 *
 * 1. **队列是「本次会话的快照」**，评完分的卡从本地列表里移除，不重新拉队列。
 *    否则每评一张都会触发一次刷新，卡片顺序跳动、刚看完的词可能又冒出来，
 *    用户会以为评分没生效。
 * 2. **每张卡记录「看了多久」并上报**。秒过的卡在统计里很明显，是
 *    「其实没在读」的唯一可观测信号 —— 光看正确率看不出来。
 * 3. **四个按钮上直接写下次间隔**（服务端算好的 `intervals`）。点之前就知道
 *    后果，才敢点 `easy`；这也是 Anki 用户最依赖的一个细节。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import AccentPhonetics from '../components/AccentPhonetics'
import AppNav from '../components/AppNav'
import { api, errorText } from '../lib/api'
import { phoneticNote } from '../lib/phonetic'
import type { ReviewCard, SrsGrade } from '../lib/types'
import { SRS_GRADES, SRS_GRADE_KEY, SRS_GRADE_LABEL } from '../lib/types'

/** 「3 天后」这种人话。服务端只给四个按钮的间隔，历史/到期时间在前端算。 */
function relativeTo(iso: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '—'
  const min = (t - Date.now()) / 60000
  if (min < 1) return '现在'
  if (min < 60) return `${Math.round(min)} 分钟后`
  const hours = min / 60
  if (hours < 23) return `${Math.round(hours)} 小时后`
  const days = hours / 24
  if (days < 1.5) return '明天'
  if (days < 30) return `${Math.round(days)} 天后`
  if (days < 365) return `${(days / 30).toFixed(1)} 个月后`
  return `${(days / 365).toFixed(1)} 年后`
}

/**
 * 把例句里的目标词挖掉。用于「先看语境再猜词」的提示模式。
 *
 * 找不到 `surface` 就退而找 `lemma`（划到的可能是变形，句子里的形态未必一致）；
 * 都找不到就原样返回并让调用方提示 —— 宁可露出来，也不要默默显示一句
 * 把答案写在上面的提示。
 */
function maskWord(sentence: string, surface: string, lemma: string): { text: string; masked: boolean } {
  const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  for (const w of [surface, lemma]) {
    if (!w) continue
    const re = new RegExp(escape(w), 'gi')
    if (re.test(sentence)) {
      return { text: sentence.replace(re, '﹏﹏'), masked: true }
    }
  }
  return { text: sentence, masked: false }
}

function GradeButton({
  grade,
  next,
  disabled,
  onClick,
}: {
  grade: SrsGrade
  next: string | undefined
  disabled: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      className="er-grade"
      data-grade={grade}
      data-testid={`grade-${grade}`}
      disabled={disabled}
      onClick={onClick}
      title={`${SRS_GRADE_LABEL[grade]}（快捷键 ${Object.keys(SRS_GRADE_KEY).find((k) => SRS_GRADE_KEY[k] === grade)}）`}
    >
      <span className="er-grade-name">{SRS_GRADE_LABEL[grade]}</span>
      <span className="er-grade-next">{next ?? '—'}</span>
    </button>
  )
}

export default function ReviewPage() {
  const qc = useQueryClient()
  const queue = useQuery({
    queryKey: ['review-queue'],
    queryFn: () => api.reviewQueue(),
  })
  const stats = useQuery({ queryKey: ['review-stats'], queryFn: api.reviewStats })

  /** 本次会话的队列快照：评完就从这里删，不回拉 */
  const [cards, setCards] = useState<ReviewCard[]>([])
  const [idx, setIdx] = useState(0)
  const [revealed, setRevealed] = useState(false)
  const [hintMode, setHintMode] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [tally, setTally] = useState({ again: 0, hard: 0, good: 0, easy: 0 })
  const [flash, setFlash] = useState<string | null>(null)
  /**
   * 本轮**评过的**卡里最早的那个到期时间。
   *
   * 为什么不能只靠服务端的 `next_due`：队列是本轮会话的快照（见文件头），
   * 那个值是**装载时**算的 —— 当时这些卡都还没到期的下一天，所以它是 null。
   * 于是「把队列评完」那一屏本来会写「下次到期：—」，而用户刚评完、最想知道
   * 的恰恰是这句话。评分响应里带着每张卡评分后的 `due`，在这里取最小值即可，
   * 既准确又不用回拉队列（回拉会导致卡片列表重置）。
   */
  const [soonestDue, setSoonestDue] = useState<string | null>(null)
  const shownAt = useRef<number>(Date.now())

  /** 队列首次到达（或用户手动重取）时装载快照 */
  useEffect(() => {
    const data = queue.data
    if (!data) return
    setCards(data.items)
    setIdx(0)
    setRevealed(false)
    setErr(null)
    setSoonestDue(null)
    shownAt.current = Date.now()
  }, [queue.data])

  const grade = useMutation({
    mutationFn: ({ id, g, ms }: { id: number; g: SrsGrade; ms: number }) =>
      api.reviewGrade(id, g, ms),
    onSuccess: (card, vars) => {
      const next = card.review.due_label
      setFlash(`${card.lemma} · 下次 ${next}`)
      window.setTimeout(() => setFlash(null), 1800)
      setTally((t) => ({ ...t, [vars.g]: t[vars.g] + 1 }))
      if (card.review.due) {
        setSoonestDue((s) => (s && s < card.review.due! ? s : card.review.due!))
      }
      setIdx((i) => i + 1)
      setRevealed(false)
      setHintMode(false)
      shownAt.current = Date.now()
      setErr(null)
      void qc.invalidateQueries({ queryKey: ['review-stats'] })
      void qc.invalidateQueries({ queryKey: ['vocab-stats'] })
      void qc.invalidateQueries({ queryKey: ['vocab'] })
    },
    onError: (e) => setErr(errorText(e)),
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.reviewSuspend(id),
    onSuccess: () => {
      setIdx((i) => i + 1)
      setRevealed(false)
      setFlash('已暂停复习这个词')
      window.setTimeout(() => setFlash(null), 1800)
      void qc.invalidateQueries({ queryKey: ['review-stats'] })
    },
    onError: (e) => setErr(errorText(e)),
  })

  const card = cards[idx] ?? null

  const submit = useCallback(
    (g: SrsGrade) => {
      if (!card || grade.isPending) return
      grade.mutate({ id: card.id, g, ms: Date.now() - shownAt.current })
    },
    [card, grade],
  )

  const reload = useCallback(() => {
    void queue.refetch()
    void stats.refetch()
    setTally({ again: 0, hard: 0, good: 0, easy: 0 })
  }, [queue, stats])

  /** 快捷键。只在没有表单焦点时生效（这一页没有输入框，但保险）。 */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      if (el && /INPUT|TEXTAREA|SELECT/.test(el.tagName)) return
      if (!card) return
      if (!revealed) {
        if (e.key === ' ' || e.key === 'Enter') {
          e.preventDefault()
          setRevealed(true)
        }
        return
      }
      const g = SRS_GRADE_KEY[e.key]
      if (g) {
        e.preventDefault()
        submit(g)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [card, revealed, submit])

  const reviewed = tally.again + tally.hard + tally.good + tally.easy
  const left = Math.max(0, cards.length - idx)

  const masked = useMemo(() => {
    if (!card || !hintMode || !card.sentence) return null
    return maskWord(card.sentence, card.surface, card.lemma)
  }, [card, hintMode])

  /** 例句出处：优先用「那句自己的文章」，没有才退回首次入册那篇。 */
  const srcArticleId = card
    ? (card.sentence_article_id ?? card.article_id)
    : null

  const maxForecast = useMemo(
    () => Math.max(1, ...(stats.data?.forecast ?? []).map((f) => f.due)),
    [stats.data],
  )

  /**
   * 清空态里那句「下次到期」。
   *
   * 两个来源取较早的那个：本轮刚评完的卡（`soonestDue`）与装载时服务端算的
   * `next_due`（适用于「一进来队列就是空的」）。ISO 字符串同格式可以直接比。
   */
  const nextUp = useMemo(() => {
    const fromServer = queue.data?.next_after_queue ?? queue.data?.next_due ?? null
    if (soonestDue && fromServer) return soonestDue < fromServer ? soonestDue : fromServer
    return soonestDue ?? fromServer
  }, [soonestDue, queue.data])

  return (
    <div className="min-h-screen">
      <AppNav />
      <main className="mx-auto max-w-[820px] px-4 py-6">
        {/* ---------------------------------------------------------- 头部 */}
        <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-lg font-semibold" style={{ color: 'var(--ink)' }}>
            间隔复习
          </h1>
          <span className="er-muted text-xs" data-testid="review-left">
            {left > 0 ? `本轮还剩 ${left} 张` : '本轮已清空'}
          </span>
          {reviewed > 0 ? (
            <span className="er-muted text-xs" data-testid="review-tally">
              已评 {reviewed} · 忘了 {tally.again} · 记得 {tally.good + tally.easy}
            </span>
          ) : null}
          <button className="er-btn ml-auto !px-2.5 !py-1 !text-[11px]" onClick={reload}>
            重新取队列
          </button>
        </div>

        {/* ---------------------------------------------------- 七日预测条 */}
        {stats.data ? (
          <div className="er-card mb-4 px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
              <span style={{ color: 'var(--ink)' }}>
                待复习 <b data-testid="stat-due">{stats.data.due_now}</b>
              </span>
              <span className="er-muted">
                新词 <b style={{ color: 'var(--ink-soft)' }}>{stats.data.new_available}</b>
              </span>
              <span className="er-muted">
                生词本共 <b style={{ color: 'var(--ink-soft)' }}>{stats.data.total}</b>
              </span>
              <span className="er-muted">
                今日已评 <b style={{ color: 'var(--ink-soft)' }}>{stats.data.today.reviews}</b>
              </span>
              {stats.data.retention_7d != null ? (
                <span className="er-muted">
                  近七日记住率{' '}
                  <b style={{ color: 'var(--ink-soft)' }}>
                    {Math.round(stats.data.retention_7d * 100)}%
                  </b>
                </span>
              ) : null}
              {stats.data.suspended ? (
                <span className="er-muted">已暂停 {stats.data.suspended}</span>
              ) : null}
            </div>
            <div className="mt-3 flex items-end gap-1" title="未来七天每天到期的词数">
              {stats.data.forecast.map((f) => (
                <div key={f.date} className="flex flex-1 flex-col items-center gap-1">
                  <div
                    className="w-full rounded-sm"
                    style={{
                      height: `${Math.max(3, (f.due / maxForecast) * 34)}px`,
                      background: f.due ? 'var(--link)' : 'var(--rule)',
                      opacity: f.due ? 0.65 : 1,
                    }}
                    title={`${f.date} · ${f.due} 个`}
                  />
                  <span className="er-muted text-[10px]">{f.date.slice(5)}</span>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {err ? (
          <p className="mb-3 text-sm" style={{ color: '#d1242f' }}>
            {err}
          </p>
        ) : null}

        {/* ------------------------------------------------------------ 卡片 */}
        {queue.isLoading ? (
          <p className="er-muted py-16 text-center text-sm">正在取队列…</p>
        ) : !card ? (
          <div className="er-card px-6 py-12 text-center" data-testid="review-done">
            <p className="text-base font-semibold" style={{ color: 'var(--ink)' }}>
              {reviewed > 0 ? '本轮复习完成' : '现在没有到期的词'}
            </p>
            <p className="er-muted mt-2 text-sm">
              下次到期：
              <b style={{ color: 'var(--ink-soft)' }}>{relativeTo(nextUp)}</b>
            </p>
            <p className="er-muted mt-1 text-xs">
              复习间隔最长 {stats.data?.interval_cap_days ?? 365} 天；学习阶段按
              {(stats.data?.learn_steps_min ?? [1, 10]).join(' / ')} 分钟推进。
            </p>
            <div className="mt-4 flex justify-center gap-2">
              <button className="er-btn !px-3 !py-1.5" onClick={reload}>
                看看有没有新的
              </button>
              <Link to="/vocab" className="er-btn !px-3 !py-1.5 no-underline">
                去生词本
              </Link>
            </div>
          </div>
        ) : (
          <div className="er-card px-6 py-6" data-testid="review-card" data-vocab={card.id}>
            <div className="flex items-center gap-2 text-[11px]">
              <span className="er-chip">{card.review.state_label}</span>
              <span className="er-muted">{card.review.progress}</span>
              <span className="er-muted ml-auto">
                {idx + 1} / {cards.length}
              </span>
            </div>

            {/* ---- 正面 ---- */}
            <div className="mt-6 text-center">
              <p
                className="text-4xl font-semibold leading-tight"
                style={{ color: 'var(--ink)', fontFamily: 'var(--er-serif)' }}
                data-testid="card-lemma"
              >
                {card.lemma}
              </p>
              <div className="mt-2 flex flex-wrap items-center justify-center gap-2 text-sm">
                {/* 正面给发音是正确的：卡正面本来就摆着音标，听一遍不泄露释义。
                    复习时最需要听音，这里缺喇叭才是真的怪。
                    英美各一个（音频不预热，点哪个才下哪个）；M12 起两条音标
                    并排显示，点哪个喇叭就能看哪个的读音。 */}
                <AccentPhonetics
                  uk={card.phonetic_uk}
                  us={card.phonetic_us}
                  audios={card.audios}
                  word={card.lemma}
                  compact
                  className="text-sm"
                  phoneticStyle={{ color: 'var(--ink-soft)' }}
                  emptyText=""
                />
                {card.pos ? (
                  <span style={{ color: card.phonetic ? 'var(--ink-soft)' : 'var(--ink-faint)' }}>
                    {card.pos}
                  </span>
                ) : null}
              </div>
              {phoneticNote(card.lemma, card.phonetic_of) ? (
                <p className="er-muted mt-1 text-[11px]">
                  {phoneticNote(card.lemma, card.phonetic_of)}；划到的是 {card.surface}
                </p>
              ) : null}
              {!revealed ? (
                <button
                  className="er-btn mt-5 !px-3 !py-1 !text-[11px]"
                  data-on={hintMode ? '1' : undefined}
                  onClick={() => setHintMode((v) => !v)}
                  title="先看这句原文（目标词已挖掉），再决定怎么答"
                >
                  {hintMode ? '收起例句提示' : '显示例句提示'}
                </button>
              ) : null}
              {!revealed && hintMode && masked ? (
                <p
                  className="mx-auto mt-3 max-w-[560px] text-left text-[13px] leading-relaxed"
                  style={{ color: 'var(--ink-soft)', fontFamily: 'var(--er-serif)' }}
                  data-testid="card-hint"
                >
                  {masked.text}
                  {!masked.masked ? (
                    <span className="er-muted ml-1 text-[11px]">（该句里没有原词形）</span>
                  ) : null}
                </p>
              ) : null}
            </div>

            {/* ---- 背面 ---- */}
            {revealed ? (
              <div className="mt-6 border-t pt-5" style={{ borderColor: 'var(--rule)' }}>
                <p className="text-base" style={{ color: 'var(--ink)' }} data-testid="card-meaning">
                  {card.meaning_cn || '（没有释义）'}
                </p>
                {card.meaning_in_ctx ? (
                  <p className="mt-1.5 text-[13px]" style={{ color: 'var(--ink-soft)' }}>
                    文中含义：{card.meaning_in_ctx}
                  </p>
                ) : null}
                {card.meaning_en ? (
                  <p className="er-muted mt-1 text-[12px]">{card.meaning_en}</p>
                ) : null}
                {card.example ? (
                  <p className="mt-3 text-[13px]" style={{ color: 'var(--ink-soft)' }}>
                    <span className="er-muted mr-1 text-[11px]">例句</span>
                    {card.example}
                  </p>
                ) : null}
                {card.sentence ? (
                  <p
                    className="mt-3 border-l-2 pl-3 text-[13px] leading-relaxed"
                    style={{
                      borderColor: 'var(--rule)',
                      color: 'var(--ink-soft)',
                      fontFamily: 'var(--er-serif)',
                    }}
                    data-testid="card-sentence"
                  >
                    {card.sentence}
                    {srcArticleId != null ? (
                      <Link
                        to={`/a/${srcArticleId}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="er-muted ml-2 text-[11px] no-underline hover:underline"
                      >
                        {(card.sentence_article_title ?? `文章 #${srcArticleId}`).slice(0, 28)} ↗
                      </Link>
                    ) : null}
                  </p>
                ) : null}
                {card.note ? (
                  <p className="mt-3 text-[13px]" style={{ color: 'var(--ink-soft)' }}>
                    <span className="er-muted mr-1 text-[11px]">批注</span>
                    {card.note}
                  </p>
                ) : null}
              </div>
            ) : null}

            {/* ---- 操作区 ---- */}
            <div className="mt-6">
              {!revealed ? (
                <button
                  className="er-btn er-btn-primary w-full !py-2.5 !text-sm"
                  data-testid="reveal"
                  onClick={() => setRevealed(true)}
                >
                  显示答案
                  <span className="ml-2 text-[11px] font-normal opacity-70">空格</span>
                </button>
              ) : (
                <div className="grid grid-cols-4 gap-2" data-testid="grade-row">
                  {SRS_GRADES.map((g) => (
                    <GradeButton
                      key={g}
                      grade={g}
                      next={card.intervals?.[g]}
                      disabled={grade.isPending}
                      onClick={() => submit(g)}
                    />
                  ))}
                </div>
              )}
              <div className="mt-3 flex items-center justify-between">
                <button
                  className="er-muted text-[11px] underline-offset-2 hover:underline"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(card.id)}
                  title="这个词不再进入复习队列（生词本里仍保留）"
                >
                  暂停复习这个词
                </button>
                <span className="er-muted text-[11px]" data-testid="card-flash">
                  {flash ?? (revealed ? '按 1~4 评分' : '按空格翻面')}
                </span>
              </div>
            </div>
          </div>
        )}

        <p className="er-muted mt-4 text-[11px]">
          评分决定下次什么时候再问：忘了 → 1 分钟后重来；记得 → 间隔按难度因子放大。
          队列里「从没复习过」的新词一轮最多 {queue.data?.new_limit ?? 20} 个，
          到期的旧词不限量。
        </p>
      </main>
    </div>
  )
}
