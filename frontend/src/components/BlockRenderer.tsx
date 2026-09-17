/**
 * 正文渲染。
 *
 * `blocks.text` 是纯文本 —— 渲染层不做任何富文本解析。
 * M3 的高亮同样基于「文本 + 区间」重建：这里只会产出 <mark>，
 * 绝不会把高亮信息写回正文数据（design 红线 2）。
 *
 * 每个块都带 `data-block-id`：阅读进度的位置采集（useReadingProgress）
 * 与划词的选区定位都靠它，不要省略。
 */

import { useState } from 'react'

import { splitByHighlights } from '../lib/selection'
import type { Block, Highlight } from '../lib/types'

export type RenderItem =
  | { kind: 'text'; block: Block }
  | { kind: 'image'; block: Block; caption: Block | null }
  | { kind: 'caption'; block: Block }

/** 把紧跟在图片后的 caption 块并进 figure，省掉一层视觉割裂。 */
export function buildRenderItems(blocks: Block[]): RenderItem[] {
  const out: RenderItem[] = []
  for (let i = 0; i < blocks.length; i += 1) {
    const b = blocks[i]
    // 标题块（M9）不在这里渲染：它要排在「标题 + 元信息 + 操作按钮」那个 header
    // 里，由阅读页自己画（见 Reader）。这里如果再输出一遍，标题就会出现两次。
    // 它仍在 blocks 里，所以划词偏移、高亮重建、深链定位照旧按块走。
    if (b.type === 'title') continue
    if (b.type === 'image') {
      const next = blocks[i + 1]
      if (next && next.type === 'caption') {
        out.push({ kind: 'image', block: b, caption: next })
        i += 1
        continue
      }
      out.push({ kind: 'image', block: b, caption: null })
      continue
    }
    if (b.type === 'caption') {
      out.push({ kind: 'caption', block: b })
      continue
    }
    out.push({ kind: 'text', block: b })
  }
  return out
}

/**
 * 按高亮区间把一段文字渲染出来。
 *
 * `base` 是这段文字在所属块内的起始偏移。列表块会被拆成多个 <li>，
 * 每一行都有自己的 base —— 不做这层平移的话，第二行之后的高亮全会错位。
 *
 * 导出是为了阅读页的标题：标题块（M9）不在这个组件里遍历，但要和正文
 * **用同一套渲染**，否则「标题上的高亮」会是另一套实现，迟早对不上。
 */
export function TextWithHighlights({
  text,
  highlights,
  base = 0,
  activeId,
  focusId,
  onPick,
}: {
  text: string
  highlights: Highlight[]
  base?: number
  activeId?: number | null
  /** 深链定位的那一条（见 Reader 的 focusHighlight）：渲染层给它打 data-focus，闪两下 */
  focusId?: number | null
  onPick?: (h: Highlight) => void
}) {
  if (!text) return null
  if (!highlights.length) return <>{text}</>

  const local = highlights
    .filter((h) => h.start_offset < base + text.length && h.end_offset > base)
    .map((h) => ({
      ...h,
      start_offset: Math.max(0, h.start_offset - base),
      end_offset: Math.min(text.length, h.end_offset - base),
    }))
  if (!local.length) return <>{text}</>

  return (
    <>
      {splitByHighlights(text, local).map((seg, i) =>
        seg.highlight ? (
          <mark
            key={i}
            data-hl-id={seg.highlight.id}
            // 底色按 kind 分两档：单词一色、短语/句子另一色（见 index.css）。
            // 长句里再划几个词时，「哪个词查过译」只能靠颜色区分。
            data-kind={seg.highlight.kind}
            data-active={seg.highlight.id === activeId ? '1' : undefined}
            data-focus={seg.highlight.id === focusId ? '1' : undefined}
            title={seg.highlight.note || seg.highlight.kind}
            onClick={
              // id < 0 是「从检索/汇总跳过来」的定位标记（见 Reader 的 FOCUS_ID）：
              // 它不在库里，点了也没有东西可打开
              seg.highlight.id < 0
                ? undefined
                : (e) => {
                    e.stopPropagation()
                    onPick?.(seg.highlight as Highlight)
                  }
            }
          >
            {seg.text}
          </mark>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </>
  )
}

/** 列表块按行切开，同时记住每行在整块文本里的起点，供高亮平移用。 */
function listLines(text: string): { text: string; start: number }[] {
  const out: { text: string; start: number }[] = []
  const re = /[^\n]+/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) out.push({ text: m[0], start: m.index })
  return out
}

function Figure({
  block,
  caption,
  onZoom,
}: {
  block: Block
  caption: Block | null
  onZoom: (src: string, caption: string | null) => void
}) {
  const [failed, setFailed] = useState(false)
  const src = block.media_url

  return (
    <figure className="blk-figure" data-block-id={block.id}>
      {!src || failed ? (
        <div className="img-missing">
          {src ? '图片加载失败' : '图片未本地化'}
          <br />
          <span className="text-[0.9em] opacity-70">
            {(block.origin_url ?? '').slice(0, 90)}
          </span>
        </div>
      ) : (
        <img
          src={src}
          alt={block.alt ?? ''}
          loading="lazy"
          decoding="async"
          onClick={() => onZoom(src, caption?.text ?? block.alt ?? null)}
          onError={() => setFailed(true)}
        />
      )}
      {caption ? (
        <figcaption data-block-id={caption.id}>{caption.text}</figcaption>
      ) : null}
    </figure>
  )
}

interface TextBlockProps {
  block: Block
  highlights: Highlight[]
  activeId?: number | null
  focusId?: number | null
  onPick?: (h: Highlight) => void
}

function TextBlock({ block, highlights, activeId, focusId, onPick }: TextBlockProps) {
  const text = block.text ?? ''
  const shared = { highlights, activeId, focusId, onPick }

  switch (block.type) {
    case 'heading': {
      const level = Math.min(Math.max(block.level ?? 2, 1), 6)
      const Tag = `h${level}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
      return (
        <Tag className="blk-heading" data-level={level} data-block-id={block.id}>
          <TextWithHighlights text={text} {...shared} />
        </Tag>
      )
    }
    case 'quote':
      return (
        <blockquote className="blk-quote" data-block-id={block.id}>
          <TextWithHighlights text={text} {...shared} />
        </blockquote>
      )
    case 'list': {
      const lines = listLines(text)
      if (lines.length <= 1) {
        // 单行 list 块（提取时压平过）不值得做列表语义，退回段落更耐看
        return (
          <p data-block-id={block.id}>
            <TextWithHighlights text={text} {...shared} />
          </p>
        )
      }
      return (
        <ul className="blk-list" data-block-id={block.id}>
          {lines.map((ln, i) => (
            <li key={i}>
              <TextWithHighlights text={ln.text} base={ln.start} {...shared} />
            </li>
          ))}
        </ul>
      )
    }
    case 'table':
      return (
        <div className="blk-table" data-block-id={block.id}>
          <TextWithHighlights text={text} {...shared} />
        </div>
      )
    case 'video':
      return (
        <div className="blk-video" data-block-id={block.id}>
          视频（未下载）：{text || block.origin_url}
        </div>
      )
    case 'placeholder':
      return null
    default:
      return (
        <p data-block-id={block.id}>
          <TextWithHighlights text={text} {...shared} />
        </p>
      )
  }
}

export default function BlockRenderer({
  items,
  onZoom,
  highlightsByBlock,
  activeHighlightId,
  focusHighlightId,
  onPickHighlight,
}: {
  items: RenderItem[]
  onZoom: (src: string, caption: string | null) => void
  /** M3：按块归拢好的痕迹，用于重建高亮 */
  highlightsByBlock?: Map<number, Highlight[]>
  activeHighlightId?: number | null
  /** M4：深链定位的那一条，渲染时闪两下（见 Reader 的 focusHighlight） */
  focusHighlightId?: number | null
  onPickHighlight?: (h: Highlight) => void
}) {
  return (
    <div className="prose-en">
      {items.map((it) => {
        if (it.kind === 'image') {
          return (
            <Figure
              key={it.block.id}
              block={it.block}
              caption={it.caption}
              onZoom={onZoom}
            />
          )
        }
        if (it.kind === 'caption') {
          return (
            <p className="blk-caption" data-block-id={it.block.id} key={it.block.id}>
              <TextWithHighlights
                text={it.block.text ?? ''}
                highlights={highlightsByBlock?.get(it.block.id) ?? []}
                activeId={activeHighlightId}
                focusId={focusHighlightId}
                onPick={onPickHighlight}
              />
            </p>
          )
        }
        const hl = highlightsByBlock?.get(it.block.id) ?? []
        // 没有痕迹的块不传引用，避免 TextWithHighlights 每次重建数组
        return (
          <TextBlock
            key={it.block.id}
            block={it.block}
            highlights={hl.length ? hl : EMPTY}
            activeId={activeHighlightId}
            focusId={focusHighlightId}
            onPick={onPickHighlight}
          />
        )
      })}
    </div>
  )
}

const EMPTY: Highlight[] = []
