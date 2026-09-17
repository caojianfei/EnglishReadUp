/** 阅读偏好：只落在 localStorage，不进后端 —— 它是「这台设备怎么显示」，不是学习数据。 */

export type ThemeName = 'light' | 'sepia' | 'dark'

export interface ReadingPrefs {
  fontSize: number
  lineHeight: number
  /** 正文最大宽度（px） */
  width: number
  /** 右侧 AI 面板宽度（px）。拖它的左边缘改，双击复位 */
  panelWidth: number
  theme: ThemeName
  /** 是否显示原文图片 */
  showImages: boolean
}

export const FONT_SIZES = [16, 17, 18, 19, 20, 21, 22, 24, 26] as const
export const LINE_HEIGHTS = [1.5, 1.7, 1.9, 2.1] as const
export const WIDTHS = [
  { label: '窄', value: 600 },
  { label: '中', value: 700 },
  { label: '宽', value: 840 },
  { label: '全', value: 1040 },
] as const

/** 右栏宽度的默认值与上下限。上限还要看窗口 —— 正文不能被挤没。 */
export const PANEL_DEFAULT_W = 380
export const PANEL_MIN_W = 300
export const PANEL_MAX_W = 760

/**
 * 夹住面板宽度。
 *
 * 上界随窗口收缩：窗口只有 900px 宽时还允许拉到 760 的话，正文只剩一条缝。
 * 窗口窄到连下界都放不下时保底给下界，否则会出现负宽度。
 */
export function clampPanelWidth(w: number): number {
  const room = typeof window === 'undefined' ? PANEL_MAX_W : window.innerWidth - 360
  const max = Math.max(PANEL_MIN_W, Math.min(PANEL_MAX_W, room))
  return Math.round(Math.min(Math.max(w, PANEL_MIN_W), max))
}

export const THEMES: { value: ThemeName; label: string }[] = [
  { value: 'light', label: '浅色' },
  { value: 'sepia', label: '米色' },
  { value: 'dark', label: '深色' },
]

export const DEFAULT_PREFS: ReadingPrefs = {
  fontSize: 18,
  lineHeight: 1.7,
  width: 700,
  panelWidth: PANEL_DEFAULT_W,
  theme: 'light',
  showImages: true,
}

const KEY = 'er:prefs'

export function loadPrefs(): ReadingPrefs {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return DEFAULT_PREFS
    const parsed = JSON.parse(raw) as Partial<ReadingPrefs>
    return { ...DEFAULT_PREFS, ...parsed }
  } catch {
    return DEFAULT_PREFS
  }
}

export function savePrefs(p: ReadingPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(p))
  } catch {
    /* ignore */
  }
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds < 60) return `${seconds || 0} 秒`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  return `${h} 小时 ${m % 60} 分`
}

export function formatWords(n: number | null | undefined): string {
  return n ? `${n} 词` : '—'
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00` : iso)
  if (Number.isNaN(d.getTime())) return iso
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(d)
}
