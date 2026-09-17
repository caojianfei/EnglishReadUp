/** 阅读工具栏：主题、字号、行距、栏宽、图片开关。全部即时生效并落 localStorage。 */

import { Link } from 'react-router-dom'

import { FONT_SIZES, LINE_HEIGHTS, THEMES, WIDTHS, formatDuration } from '../lib/prefs'
import type { ReadingPrefs, ThemeName } from '../lib/prefs'

export default function Toolbar({
  prefs,
  onChange,
  pct,
  seconds,
  isOffline,
}: {
  prefs: ReadingPrefs
  onChange: (patch: Partial<ReadingPrefs>) => void
  pct: number
  seconds: number
  isOffline: boolean
}) {
  const sizeIndex = FONT_SIZES.indexOf(prefs.fontSize as (typeof FONT_SIZES)[number])
  const stepSize = (dir: -1 | 1) => {
    const idx = sizeIndex < 0 ? 2 : sizeIndex
    const next = Math.min(Math.max(idx + dir, 0), FONT_SIZES.length - 1)
    onChange({ fontSize: FONT_SIZES[next] })
  }

  return (
    <header className="er-toolbar sticky top-0 z-30">
      <div className="mx-auto flex max-w-[1180px] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2">
        <Link to="/" className="er-btn shrink-0" title="返回文章列表">
          ← 返回
        </Link>

        <div className="flex min-w-[120px] flex-1 items-center gap-3">
          <span className="er-progress-track h-1.5 w-full max-w-[240px] overflow-hidden rounded-full">
            <span className="er-progress-fill block h-full" style={{ width: `${pct * 100}%` }} />
          </span>
          <span className="er-muted shrink-0 text-xs tabular-nums">
            {Math.round(pct * 100)}% · {formatDuration(seconds)}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div className="flex items-center gap-1">
            <button className="er-btn" onClick={() => stepSize(-1)} title="缩小字号">
              A−
            </button>
            <span className="er-muted w-8 text-center text-xs tabular-nums">{prefs.fontSize}</span>
            <button className="er-btn" onClick={() => stepSize(1)} title="放大字号">
              A+
            </button>
          </div>

          <div className="flex items-center gap-1" title="行距">
            {LINE_HEIGHTS.map((lh) => (
              <button
                key={lh}
                className="er-btn"
                data-on={prefs.lineHeight === lh || undefined}
                onClick={() => onChange({ lineHeight: lh })}
              >
                {lh}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-1" title="栏宽">
            {WIDTHS.map((w) => (
              <button
                key={w.value}
                className="er-btn"
                data-on={prefs.width === w.value || undefined}
                onClick={() => onChange({ width: w.value })}
              >
                {w.label}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-1" title="主题">
            {THEMES.map((t) => (
              <button
                key={t.value}
                className="er-swatch"
                data-theme-swatch={t.value}
                data-on={prefs.theme === t.value || undefined}
                title={t.label}
                onClick={() => onChange({ theme: t.value as ThemeName })}
              />
            ))}
          </div>

          <button
            className="er-btn"
            data-on={prefs.showImages || undefined}
            onClick={() => onChange({ showImages: !prefs.showImages })}
            title="显示 / 隐藏原文图片"
          >
            {prefs.showImages ? '含图' : '纯文'}
          </button>

          {isOffline ? (
            <span className="er-chip" title="网络不可用，正在显示本地缓存内容">
              离线阅读
            </span>
          ) : null}
        </div>
      </div>
    </header>
  )
}
