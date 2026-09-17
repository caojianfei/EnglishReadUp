/**
 * 发音按钮。
 *
 * 音频**一律由后端本地化**（`data/audio/`），前端只拿一个本机地址，
 * 永远不直连有道、也不打 Wikimedia。所以这里不消费任何外链 URL ——
 * 要么给 `word` 由本组件拼播放地址，要么给后端返回的 `src`。
 *
 * 单独抽成组件是因为它现在有三个消费方（划词浮层、AI 词卡、生词本/复习卡），
 * 而「播放中 / 不可用」这两个状态在哪儿都要一致。
 */

import { useState } from 'react'

const C = {
  faint: 'var(--ink-faint)',
  rule: 'var(--rule)',
  link: 'var(--link)',
}

export const AUDIO_ACCENT_LABEL: Record<string, string> = {
  us: '美',
  uk: '英',
  other: '',
}

/**
 * 后端播放端点。单词里可能有 `/`、`'` 这类字符，必须转义。
 *
 * `accent` 决定听英音还是美音（`?accent=uk|us`）；不给就由后端的
 * `AUDIO_ACCENT` 决定。同一个词的两种口音是**两份独立下载的文件**，
 * 各自点各自的。
 */
export function audioUrl(word: string, accent?: string | null): string {
  return `/api/dict/${encodeURIComponent(word)}/audio${accent ? `?accent=${encodeURIComponent(accent)}` : ''}`
}

export function AudioButton({
  word,
  src,
  accent,
  origin,
  status,
  compact,
}: {
  /** 词形。给了就按它拼播放地址 */
  word?: string | null
  /** 后端已经算好的播放地址；与 word 二选一 */
  src?: string | null
  /** us | uk | other */
  accent?: string | null
  /** youdao | wiktionary，只进 tooltip，界面不展示 */
  origin?: string | null
  /** ok|pending|missing。missing 是**终态**：确认没有录音就不画按钮，
   *  免得用户点出一句「音频不可用」。pending 和未知都照画 —— 
   *  后端会当场去取，点下去只是稍等。 */
  status?: string | null
  /** 紧凑样式：生词本/复习卡的列表里用 */
  compact?: boolean
}) {
  const [playing, setPlaying] = useState(false)
  const [failed, setFailed] = useState(false)

  const url = src || (word ? audioUrl(word, accent) : null)
  // missing 是**终态**：确认这个口音没有录音，画一个必然 404 的按钮只会让人以为坏了
  if (!url || status === 'missing') return null

  const label = AUDIO_ACCENT_LABEL[accent ?? 'us'] ?? ''

  const play = () => {
    if (playing) return
    setPlaying(true)
    const el = new Audio(url)
    el.onended = () => setPlaying(false)
    el.onerror = () => {
      setPlaying(false)
      setFailed(true)
    }
    void el.play().catch(() => {
      setPlaying(false)
      setFailed(true)
    })
  }

  const tip = failed
    ? '这个录音取不到'
    : `播放发音${label ? `（${label}音）` : ''}${origin ? ` · 来源 ${origin}` : ''}`

  return (
    <button
      type="button"
      onClick={play}
      data-testid="play-audio"
      data-accent={accent || 'us'}
      data-origin={origin || ''}
      data-url={url}
      className={
        compact
          ? 'shrink-0 rounded px-1 text-xs transition-opacity hover:opacity-70'
          : 'rounded px-1.5 py-0.5 text-xs transition-opacity hover:opacity-70'
      }
      style={{ border: `1px solid ${C.rule}`, color: failed ? C.faint : C.link }}
      title={tip}
    >
      {failed ? '音频不可用' : playing ? '播放中…' : `🔊${label ? ` ${label}` : ''}`}
    </button>
  )
}
