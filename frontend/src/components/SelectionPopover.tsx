/**
 * 划词浮层。
 *
 * 两种形态，由选区类别决定：
 *   - 单个词 → 词卡（英美音标 + 词性 + 释义），划完立刻查
 *   - 短语/句子/段落 → 只给一个【解析】按钮：不自动调模型，也不落痕 ——
 *     没点过【解析】就当没划过（那类卡片上没有「取消标记」可点）
 *
 * 「词自动查、句不自动查」是刻意的：划一个词就是想查它，
 * 而拖拽选一段往往是误触或先看看长度，自动解析纯属浪费。
 *
 * 这里的「单个词」是**完整单词**：只划中词里几个字母时，区间会被
 * `snapToWholeWord` 扩到整个词（并在这里标一句「已按整词查」）——
 * 半截字母拿去查词典只会得到胡说八道。
 *
 * M10 起词卡的数据全部来自**本地词典库**（ECDICT 释义 + ipa-dict 英美音标），
 * 不再调模型 —— 所以卡片上没有「在这句里的意思」「例句」「搭配」这些
 * 只有模型才给得出的栏目，也没有模型名与耗时。
 *
 * 离线词典偶尔有释义不对的词条：中文释义旁的【纠错】可以就地改掉。修正存在
 * 后端（`dict_overrides`），叠在词典之上 —— 再查这个词、生词本、复习卡都用
 * 修正值；【恢复词典释义】随时退回原文。
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api, dropDictCache, errorText } from '../lib/api'
import { phoneticNote } from '../lib/phonetic'
import type { DictSetup, SelectionInfo, WordEntry } from '../lib/types'
import AccentPhonetics from './AccentPhonetics'

const WIDTH = 380
const GAP = 8
const MARGIN = 8

export interface SelectionPopoverProps {
  info: SelectionInfo
  /** 选区在视口中的位置 */
  rect: DOMRect
  /** 本地词典查出来的词条；只划中一个词时才有 */
  entry: WordEntry | null
  loading: boolean
  error: string | null
  /**
   * 选区在库里有没有对应的痕迹。
   *
   * 单词在划中的那一刻就落痕（true）；选段要等用户点过【解析】才落（false）——
   * 没落痕就没有「取消标记」可言，那个按钮因此只在 true 时出现。
   */
  marked: boolean
  /** 浮层里点【解析】：交给 Reader 去开右侧面板 */
  onAction: (action: 'explain') => void
  onDelete: () => void
  onClose: () => void
  /** 释义修正 / 恢复之后，把后端返回的新词条交回去（Reader 用它替换当前词卡） */
  onEntryChange?: (entry: WordEntry) => void
}

/**
 * 「词典库没装」时的第二行：说清接下来会发生什么。
 *
 * 服务启动时已经在**后台**补数据了（`services/dictdata.py`），所以正常情况下
 * 不该再让人去跑脚本 —— 只有自动下载失败、或被 `DICT_AUTODOWNLOAD=false` 关掉时，
 * 才把手工那条路摆出来。
 */
function SetupNote({ setup }: { setup?: DictSetup | null }) {
  const script = <code>python scripts/fetch_dicts.py</code>
  if (setup?.state === 'downloading') {
    return <>服务正在后台下载，下完重新划一次这个词就行，不用手动操作。</>
  }
  if (setup?.state === 'error') {
    return <>重启服务会自动重试，也可以在 backend/ 下跑一次 {script}。</>
  }
  if (setup?.state === 'off') {
    return <>自动下载被关掉了（DICT_AUTODOWNLOAD=false）：在 backend/ 下跑一次 {script}。</>
  }
  return <>在 backend/ 下跑一次 {script} 即可。</>
}

export default function SelectionPopover({
  info,
  rect,
  entry,
  loading,
  error,
  marked,
  onAction,
  onDelete,
  onClose,
  onEntryChange,
}: SelectionPopoverProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [placeAbove, setPlaceAbove] = useState(false)
  const [left, setLeft] = useState(0)
  const [ready, setReady] = useState(false)
  const qc = useQueryClient()

  // ---- 释义纠错
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [fixErr, setFixErr] = useState<string | null>(null)

  // 换了一个词就退出编辑，别把上一个词的草稿带过来
  useEffect(() => {
    setEditing(false)
    setFixErr(null)
  }, [entry?.word])

  const commitMeaning = async (next: string | null) => {
    if (!entry) return
    if (next !== null && !next.trim()) {
      setFixErr('释义不能为空；想退回词典原文请点「恢复词典释义」')
      return
    }
    setSaving(true)
    setFixErr(null)
    try {
      const fresh =
        next === null ? await api.resetMeaning(entry.word) : await api.setMeaning(entry.word, next)
      dropDictCache(info.surface)
      dropDictCache(entry.word)
      // 生词本里同一个词也被后端一起改了
      void qc.invalidateQueries({ queryKey: ['vocab'] })
      // 新词条里没有入册回执（纠错接口不入册），沿用这次划词时的
      onEntryChange?.({ ...fresh, enrolled: entry.enrolled })
      setEditing(false)
    } catch (e) {
      setFixErr(errorText(e))
    } finally {
      setSaving(false)
    }
  }

  // 先渲染一帧量高度，再决定放上方还是下方 —— 否则会先闪一下再跳位
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const h = el.offsetHeight
    const below = window.innerHeight - rect.bottom - GAP
    const above = rect.top - GAP
    setPlaceAbove(below < Math.min(h, 320) && above > below)
    const center = rect.left + rect.width / 2
    const max = window.innerWidth - WIDTH - MARGIN
    setLeft(Math.max(MARGIN, Math.min(center - WIDTH / 2, max)))
    setReady(true)
  }, [rect, entry, loading, error, editing])

  // 点空白处 / Esc 关闭。mousedown 而非 click：避免与选区拖拽冲突。
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const isWord = info.kind === 'word'
  const top = placeAbove ? undefined : rect.bottom + GAP
  const bottom = placeAbove ? window.innerHeight - rect.top + GAP : undefined

  // 原形与划到的形态不同（preserved → preserve）时说一声：释义与音标都是原形的
  const lemmaDiffers =
    !!entry && !!entry.lemma && entry.lemma.toLowerCase() !== info.surface.toLowerCase()

  return (
    <div
      ref={ref}
      data-testid="selection-popover"
      data-kind={info.kind}
      className="fixed z-50 overflow-auto rounded-lg shadow-xl"
      style={{
        left,
        top,
        bottom,
        width: WIDTH,
        maxHeight: 'min(70vh, 520px)',
        visibility: ready ? 'visible' : 'hidden',
        background: 'var(--paper)',
        border: '1px solid var(--rule)',
        color: 'var(--ink)',
      }}
      // 浮层内的交互不该被「点外面就关」的监听吃掉
      onMouseDown={(e) => e.stopPropagation()}
    >
      {/* 头部 */}
      <div
        className="sticky top-0 flex items-start justify-between gap-2 px-3 py-2"
        style={{ background: 'var(--paper)', borderBottom: '1px solid var(--rule)' }}
      >
        <div className="min-w-0 flex-1">
          {isWord ? (
            <div className="flex min-w-0 items-baseline gap-2">
              <span className="truncate text-base font-semibold">{info.surface}</span>
              {/* 用户只划中了词里的几个字母时，区间会被扩到整个词（selection.ts）。
                  不说一声的话，「我选的是 a，怎么查的是 Chang」会显得像个 bug。 */}
              {info.snapped ? (
                <span
                  className="shrink-0 text-[11px]"
                  style={{ color: 'var(--ink-faint)' }}
                  data-testid="snapped-note"
                >
                  已按整词查
                </span>
              ) : null}
            </div>
          ) : (
            <div className="text-xs" style={{ color: 'var(--ink-faint)' }}>
              已选 {info.surface.length} 字 ·{' '}
              {info.kind === 'sentence' ? '整句' : info.kind === 'paragraph' ? '整段' : '短语'}
              {!info.exact ? ' · 位置已自动校正' : ''}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded px-1.5 text-sm leading-6 transition-opacity hover:opacity-60"
          style={{ color: 'var(--ink-faint)' }}
          title="关闭"
          data-testid="popover-close"
        >
          ✕
        </button>
      </div>

      <div className="px-3 py-2.5">
        {/* 非词形态：只显示选中的原文摘要 + 动作 */}
        {!isWord ? (
          <>
            <p
              className="mb-2.5 max-h-28 overflow-hidden font-serif text-sm leading-relaxed"
              style={{ color: 'var(--ink-soft)' }}
            >
              {info.surface.length > 220 ? `${info.surface.slice(0, 220)}…` : info.surface}
            </p>
            <div className="flex flex-wrap gap-1.5">
              <button
                type="button"
                className="er-btn er-btn-primary !px-2.5 !py-1 !text-xs"
                data-testid="parse-action"
                onClick={() => onAction('explain')}
              >
                解析
              </button>
              {/* 没点过【解析】的选段身上什么都没有，没有东西可取消 */}
              {marked ? (
                <button type="button" className="er-btn er-btn-danger !px-2.5 !py-1 !text-xs" onClick={onDelete}>
                  取消标记
                </button>
              ) : null}
            </div>
          </>
        ) : (
          <>
            {/* 词形态：本地词典查出来的东西，毫秒级，所以没有「音标先出、释义后到」的问题 */}
            <div className="mb-2">
              <AccentPhonetics
                uk={entry?.phonetic_uk}
                us={entry?.phonetic_us}
                audios={entry?.audios}
                word={entry?.headword || info.surface}
                className="text-[15px]"
                phoneticStyle={{ color: 'var(--ink-soft)' }}
                emptyText={loading ? '音标查询中…' : '词典未收录音标'}
                source={entry?.phonetic_source ?? 'missing'}
              />
            </div>

            {error ? (
              <p className="mb-2 rounded px-2 py-1.5 text-xs" style={{ background: 'var(--code-bg)', color: 'var(--ink-soft)' }}>
                {error}
              </p>
            ) : null}

            {loading && !entry ? (
              <div className="space-y-1.5 py-1">
                <div className="h-3 w-1/2 animate-pulse rounded" style={{ background: 'var(--rule)' }} />
                <div className="h-3 w-4/5 animate-pulse rounded" style={{ background: 'var(--rule)' }} />
                <div className="h-3 w-2/3 animate-pulse rounded" style={{ background: 'var(--rule)' }} />
              </div>
            ) : null}

            {entry && entry.source === 'unavailable' ? (
              <p
                className="text-sm leading-relaxed"
                style={{ color: 'var(--ink-soft)' }}
                data-testid="dict-unavailable"
              >
                {entry.setup?.text || '本地词典库没有安装，查不到释义。'}
                <br />
                <span className="text-xs" style={{ color: 'var(--ink-faint)' }}>
                  <SetupNote setup={entry.setup} />
                </span>
              </p>
            ) : null}

            {entry && entry.source === 'missing' ? (
              <p className="text-sm leading-relaxed" style={{ color: 'var(--ink-soft)' }}>
                词典未收录这个词。
                <span className="text-xs" style={{ color: 'var(--ink-faint)' }}>
                  （数字、专名、拼写错误都会落到这里）
                </span>
              </p>
            ) : null}

            {entry && entry.source === 'ecdict' ? (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-sm font-medium">{entry.lemma}</span>
                  {entry.pos ? (
                    <span className="text-xs" style={{ color: 'var(--link)' }}>
                      {entry.pos}
                    </span>
                  ) : null}
                  {lemmaDiffers ? (
                    <span className="text-[11px]" style={{ color: 'var(--ink-faint)' }}>
                      原形
                    </span>
                  ) : null}
                  {/* 音标取自原形时要说出来，否则用户会以为 preserved 读作 /pɹəˈzɜːvd/ */}
                  {phoneticNote(entry.lemma, entry.phonetic_of) ? (
                    <span
                      className="text-[11px]"
                      style={{ color: 'var(--ink-faint)' }}
                      data-testid="phonetic-of-note"
                    >
                      {phoneticNote(entry.lemma, entry.phonetic_of)}
                    </span>
                  ) : null}
                  {entry.worth ? <span className="er-chip">重点</span> : null}
                </div>
                {editing ? (
                  <div className="space-y-1.5" data-testid="meaning-editor">
                    <textarea
                      className="er-input !py-1.5 !text-sm"
                      rows={3}
                      value={draft}
                      autoFocus
                      disabled={saving}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        // Esc 只退出编辑，不能冒泡到「Esc 关浮层」的全局监听
                        if (e.key === 'Escape') {
                          e.stopPropagation()
                          setEditing(false)
                        }
                        if (e.key === 'Enter' && !e.shiftKey) {
                          e.preventDefault()
                          void commitMeaning(draft)
                        }
                      }}
                    />
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        className="er-btn er-btn-primary !px-2.5 !py-1 !text-xs"
                        disabled={saving}
                        data-testid="meaning-save"
                        onClick={() => void commitMeaning(draft)}
                      >
                        保存
                      </button>
                      <button
                        type="button"
                        className="er-btn !px-2.5 !py-1 !text-xs"
                        disabled={saving}
                        onClick={() => setEditing(false)}
                      >
                        取消
                      </button>
                      <span className="text-[10px]" style={{ color: 'var(--ink-faint)' }}>
                        Enter 保存 · Shift+Enter 换行
                      </span>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start gap-2">
                    <p className="min-w-0 flex-1 text-sm font-medium" data-testid="dict-meaning">
                      {entry.meaning_cn || '（词典里没有中文释义）'}
                    </p>
                    <button
                      type="button"
                      className="shrink-0 text-[11px] transition-opacity hover:opacity-60"
                      style={{ color: 'var(--link)' }}
                      title="词典释义不对？改成你认为正确的（之后查这个词、生词本都会用你的版本）"
                      data-testid="meaning-fix"
                      onClick={() => {
                        setDraft(entry.meaning_cn)
                        setFixErr(null)
                        setEditing(true)
                      }}
                    >
                      纠错
                    </button>
                  </div>
                )}
                {fixErr ? (
                  <p className="text-xs" style={{ color: 'var(--ink-soft)' }}>
                    {fixErr}
                  </p>
                ) : null}
                {entry.meaning_en ? (
                  <p className="text-xs leading-relaxed" style={{ color: 'var(--ink-soft)' }}>
                    {entry.meaning_en}
                  </p>
                ) : null}
                <div className="flex flex-wrap items-center gap-2 pt-0.5 text-[10px]" style={{ color: 'var(--ink-faint)' }}>
                  <span>ECDICT</span>
                  {/* 修正过的释义要能看出来，并且随时能退回词典原文 */}
                  {entry.meaning_edited ? (
                    <>
                      <span>·</span>
                      <span
                        title={`词典原文：${entry.meaning_cn_dict || '（空）'}`}
                        data-testid="meaning-edited"
                      >
                        释义已修正
                      </span>
                      <button
                        type="button"
                        className="hover:underline"
                        style={{ color: 'var(--link)' }}
                        disabled={saving}
                        data-testid="meaning-reset"
                        onClick={() => void commitMeaning(null)}
                      >
                        恢复词典释义
                      </button>
                    </>
                  ) : null}
                  {entry.tags ? (
                    <>
                      <span>·</span>
                      <span>考纲 {entry.tags}</span>
                    </>
                  ) : null}
                  {/* 收录是自动发生的，不告诉用户的话「生词本」就显得凭空多出一条 */}
                  {entry.enrolled?.enrolled ? (
                    <>
                      <span>·</span>
                      <Link
                        to="/vocab"
                        className="no-underline hover:underline"
                        style={{ color: 'var(--link)' }}
                        data-testid="enrolled-badge"
                      >
                        {entry.enrolled.created ? '已收入生词本' : '生词本已更新'}
                      </Link>
                    </>
                  ) : null}
                </div>
              </div>
            ) : null}

            <div className="mt-2.5 flex flex-wrap gap-1.5 border-t pt-2" style={{ borderColor: 'var(--rule)' }}>
              <button
                type="button"
                className="er-btn !px-2.5 !py-1 !text-xs"
                data-testid="parse-action"
                onClick={() => onAction('explain')}
              >
                解析
              </button>
              {marked ? (
                <button type="button" className="er-btn er-btn-danger !px-2.5 !py-1 !text-xs" onClick={onDelete}>
                  取消标记
                </button>
              ) : null}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
