/**
 * 英美两条音标（+ 每条自己的喇叭）。
 *
 * 从划词浮层那份 `PhoneticRow` 抽出来的：M12 起生词本卡与复习卡也要显示两条 ——
 * 一个词在词典里本来就常是英美各一条（`sanction` 英 /sˈɑːnkʃən/、美 /ˈsænkʃən/），
 * 只显示主音标（美音优先）会把另一条藏起来：用户点「英」那个喇叭听到的读音，
 * 在卡上根本找不到。三处共用这一份实现，顺带保证口径不会漂移。
 *
 * 三条口径，与浮层一致：
 *   - **缺哪边就只显示哪边**：ipa-dict 对专名常常只收美音（`Beijing`、`Thailand`），
 *     硬凑一条出来是编造；
 *   - **喇叭跟在自己那条音标后面**：两个喇叭并排放在末尾时，用户得先数一下
 *     「左边那个是英还是美」；
 *   - **音标没有 ≠ 没录音**：录音来自有道，不依赖词典收没收这个词条，
 *     所以一条音标都没有时照样画喇叭。
 *
 * 锚点（验收脚本依赖，别随手改）：
 *   - 有音标 → `data-testid="phonetic-line"`；没有 → `data-testid="phonetic-missing"`。
 *     那个 testid 的语义就是「有音标」，脚本靠 `wait_for_selector` 等它出现 ——
 *     没音标也挂上去的话，等待会立刻命中一个空节点，断言直接拿到 None（M7 踩过）。
 *   - 每条音标自己带 `data-accent` / `data-phonetic`（词典原文，没做显示归一），
 *     供脚本逐条对账；整行上另给 `data-phonetic-uk` / `data-phonetic-us` / `data-source`。
 */

import type { CSSProperties } from 'react'

import { displayPhonetic } from '../lib/phonetic'
import type { AudioSlot } from '../lib/types'
import { AudioButton } from './AudioButton'

/** 音标来源的显示名。两个来源写法不同（一个是现代 IPA，一个是老式英标）。 */
const SOURCE_LABEL: Record<string, string> = {
  ipa: 'ipa-dict',
  ecdict: 'ECDICT',
}

export interface AccentPhoneticsProps {
  uk?: string | null
  us?: string | null
  /** 按口音索引的发音槽；缺省就只画「没有 url 可播」的那些（即不画） */
  audios?: Record<string, AudioSlot> | null
  /** 喇叭播放用的词 */
  word?: string | null
  /** 喇叭用紧凑样式（列表里用） */
  compact?: boolean
  /** 外层排版类（字号之类），两条音标都继承它 */
  className?: string
  /** 音标文字的颜色：词卡用 ink-soft，生词本/复习卡用 link */
  phoneticStyle?: CSSProperties
  /** 一条音标都没有时的文案；给空串就只画喇叭（复习卡那样） */
  emptyText?: string
  /** 音标来源；给了才渲染「音标来自词典（…）」那行说明 */
  source?: string | null
  /** 整行音标的锚点（生词本卡用它做 `vocab-phonetic`）；缺省是 `phonetic-line` */
  testId?: string
}

export default function AccentPhonetics({
  uk,
  us,
  audios,
  word,
  compact,
  className = '',
  phoneticStyle,
  emptyText = '词典未收录音标',
  source,
  testId = 'phonetic-line',
}: AccentPhoneticsProps) {
  const speaker = (accent: 'uk' | 'us') => {
    const s = audios?.[accent]
    return (
      <AudioButton
        src={s?.url ?? null}
        word={word}
        accent={accent}
        origin={s?.origin ?? null}
        status={s?.status ?? null}
        compact={compact}
      />
    )
  }

  const line = (accent: 'uk' | 'us', ipa: string, label: string) => (
    <span className="flex items-center gap-1">
      <span className="text-[0.7em] uppercase" style={{ color: 'var(--ink-faint)' }}>
        {label}
      </span>
      <span className="font-serif" style={phoneticStyle} data-accent={accent} data-phonetic={ipa}>
        {displayPhonetic(ipa)}
      </span>
      {speaker(accent)}
    </span>
  )

  if (!uk && !us) {
    return (
      <span
        className={`inline-flex flex-wrap items-center gap-x-2 gap-y-1 ${className}`}
        data-testid="phonetic-missing"
      >
        {emptyText ? (
          <span className="text-xs" style={{ color: 'var(--ink-faint)' }}>
            {emptyText}
          </span>
        ) : null}
        {speaker('uk')}
        {speaker('us')}
      </span>
    )
  }

  // 两个数据源对同一个词的标注**常常完全相同**（`ancestry` 的英美音标一字不差）。
  // 那种情况下并排两条一模一样的是噪声 —— 只显示一条，但两个喇叭都留着：
  // 音标相同不代表录音相同，英音美音是两份不同的音频。
  const sameIpa = uk && us && uk === us ? us : null

  return (
    <span
      className={`inline-flex flex-wrap items-center gap-x-3 gap-y-1 ${className}`}
      data-testid={testId}
      // `data-phonetic` 保留词典原文（归一化只作用于显示），验收脚本拿它逐字比对；
      // 英美两条各自也有锚点，供脚本分别断言。
      data-phonetic={us ?? uk}
      data-phonetic-uk={uk ?? ''}
      data-phonetic-us={us ?? ''}
      data-source={source ?? ''}
    >
      {sameIpa ? (
        <span className="flex items-center gap-1">
          <span className="font-serif" style={phoneticStyle} data-accent="us" data-phonetic={sameIpa}>
            {displayPhonetic(sameIpa)}
          </span>
          {speaker('uk')}
          {speaker('us')}
        </span>
      ) : (
        <>
          {us ? line('us', us, '美') : null}
          {uk ? line('uk', uk, '英') : null}
          {/* 音标缺了哪一边，喇叭照样给：**录音的有无与音标的有无是两件事** ——
              ipa-dict 对专名常常只收美音（Thailand、Beijing），而有道那边
              英音美音都有。把喇叭跟音标绑死等于白丢一个能播的音。 */}
          {!us ? speaker('us') : null}
          {!uk ? speaker('uk') : null}
        </>
      )}
      {source ? (
        <span className="text-[10px]" style={{ color: 'var(--ink-faint)' }} data-testid="phonetic-source-note">
          音标来自词典（{SOURCE_LABEL[source] ?? source}）
        </span>
      ) : null}
    </span>
  )
}
