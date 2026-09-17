/**
 * 选区 → 块内字符偏移。
 *
 * design 红线 2：偏移的坐标系只有 `blocks.text` 一套。高亮不写进 HTML，
 * 而是「存区间、渲染时重建」，这样原文重抓后坐标依然有效。
 *
 * 这里最容易错的一点：渲染出来的 DOM 的 textContent 不一定等于 blocks.text。
 * 例如 `list` 块渲染成 <ul><li>…</li></ul>，换行信息就没了。
 * 所以策略是「先用 DOM 精确算，再用正文切片校验」：
 *   - 切片对得上 → 直接用
 *   - 对不上 → 退化成「在正文里搜索这段文字」，且只接受唯一匹配
 *   - 两者都失败 → 返回 null，前端提示用户重新选择
 *
 * 偏移定下来之后还有一道 `snapToWholeWord`：落在词内部的片段会被扩到整个词 ——
 * 划词只认整词，见那个函数的注释。
 */

import type { Block, Highlight, HighlightKind, SelectionInfo } from './types'

/**
 * 按选区形状推断类别。
 *
 * 后端 `services/highlights.py` 的 `infer_kind` 会用 `blocks.text` 复核一次，
 * 所以这里只是「给个初值」，两边的判断口径刻意保持一致。
 */
export function inferKind(surface: string): HighlightKind {
  const s = surface.trim()
  if (!s) return 'phrase'
  if (s.length > 240) return 'paragraph'
  if (s.split(/\s+/).length === 1) {
    // 单个 token 还得**有字母**才算「一个词」。`1929` / `——` / `…` 这类划中
    // 也没有词可查，算成 word 会去查词典、进生词本，然后查回来一句空话。
    return /[A-Za-z]/.test(s) ? 'word' : 'phrase'
  }
  if (/[.!?]["'”’)]?$/.test(s) && s.length > 30) return 'sentence'
  return 'phrase'
}

/**
 * 判定「一个词里的字符」时只认字母数字。
 *
 * 撇号、连字符**不算**：`China` 是 `China's` 的前半截，但它是用户真想要的那个词，
 * 吸成 `China's` 反而是错的；`of` 在 `state-of-the-art` 里同理。英文里这两种符号
 * 两侧都能断词，保守处理只会漏吸（少改一次），激进处理会改错（多查一个词）。
 */
const WORD_CHAR = /[A-Za-z0-9]/

export interface WordSnap {
  start: number
  end: number
  surface: string
  /** true = 选区原本只是词内的几个字母，被扩到了整个词 */
  snapped: boolean
}

/**
 * 把「一个词内部的片段」吸附到整个词。
 *
 * 鼠标拖选很难精确停在词边界上：想选 `Chang`，手一抖就成了 `hang`，再抖一下就剩
 * `a`。以前这种片段会**被当成一个独立单词**去查词典、读音标、进生词本 —— 用户看到的
 * 是一张给「a」的释义卡片，生词本里还多一条查不到的词。这不是用户的意图。
 *
 * 判定刻意保守，**只处理落在同一个词里、且自己不含空格的片段**（左或右任意一侧
 * 紧挨着字母数字）。跨词、带空格的选区一律原样返回：「从 Chang 中间拖到 Shana 中间」
 * 是用户真的想选一段，改写它才是 bug。
 */
export function snapToWholeWord(
  text: string,
  start: number,
  end: number,
  surface: string,
): WordSnap {
  const keep: WordSnap = { start, end, surface, snapped: false }
  // 含空格 = 选了多个词（或一整段），不碰
  if (/\s/.test(surface)) return keep
  // 自己一个字母数字都没有：纯粹选了个标点，没有「词」可吸附
  if (!WORD_CHAR.test(surface)) return keep

  const insideLeft = start > 0 && WORD_CHAR.test(text[start - 1])
  const insideRight = end < text.length && WORD_CHAR.test(text[end])
  if (!insideLeft && !insideRight) return keep // 两边都是词边界 = 本来就是完整单词

  let s = start
  let e = end
  while (s > 0 && WORD_CHAR.test(text[s - 1])) s -= 1
  while (e < text.length && WORD_CHAR.test(text[e])) e += 1
  return { start: s, end: e, surface: text.slice(s, e), snapped: true }
}

/** 从任意节点往上找带 data-block-id 的祖先。 */
function closestBlock(node: Node | null): HTMLElement | null {
  let el: Node | null = node
  while (el && el.nodeType !== Node.ELEMENT_NODE) el = el.parentNode
  while (el) {
    if (el instanceof HTMLElement && el.dataset.blockId) return el
    el = el.parentNode
  }
  return null
}

/**
 * 计算某个 range 端点相对 root 的字符偏移。
 *
 * 端点是元素节点时，`offset` 表示「第几个子节点」而不是字符数 ——
 * 直接在 <p> 上落点（比如整段被选中）就会走这条分支，必须单独处理。
 */
function offsetAtPoint(root: Node, node: Node, offset: number): number | null {
  let acc = 0
  let found = false

  const walk = (n: Node): boolean => {
    if (n === node) {
      if (n.nodeType === Node.TEXT_NODE) {
        acc += offset
      } else {
        const kids = n.childNodes
        for (let i = 0; i < offset && i < kids.length; i += 1) {
          acc += kids[i].textContent?.length ?? 0
        }
      }
      return true
    }
    if (n.nodeType === Node.TEXT_NODE) {
      acc += n.textContent?.length ?? 0
      return false
    }
    for (const child of Array.from(n.childNodes)) {
      if (walk(child)) return true
    }
    return false
  }

  found = walk(root)
  return found ? acc : null
}

/** 把首尾空白从选区里剔除，保证偏移指向真实文字。 */
function shrink(raw: string, start: number, end: number) {
  const lead = raw.length - raw.trimStart().length
  const tail = raw.length - raw.trimEnd().length
  return {
    start: start + lead,
    end: Math.max(start + lead, end - tail),
    surface: raw.trim(),
  }
}

export interface ReadSelectionOptions {
  /** 从块 id 取正文（必须是 blocks.text，不是 DOM 文本） */
  getBlockText: (blockId: number) => string | undefined
  /** 选区必须落在这些元素内部（通常是正文容器） */
  container?: HTMLElement | null
}

/**
 * 读取当前选区，换算成 `blocks.text` 上的偏移。
 *
 * 跨块的选区会被拒绝：精读的划词/选句基本都在块内，
 * 跨块得到的是被截断的半句话，解析价值低，还容易出现两套偏移。
 */
export function readSelection(opts: ReadSelectionOptions): SelectionInfo | null {
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null

  const range = sel.getRangeAt(0)
  if (opts.container && !opts.container.contains(range.commonAncestorContainer)) return null

  const startEl = closestBlock(range.startContainer)
  const endEl = closestBlock(range.endContainer)
  if (!startEl || !endEl) return null
  if (startEl !== endEl) return null // 跨块，拒绝

  const blockId = Number(startEl.dataset.blockId)
  const text = opts.getBlockText(blockId)
  if (!text) return null

  const raw = sel.toString()
  if (!raw.trim()) return null

  // ---- 路线 1：DOM 精确偏移 ----
  const s = offsetAtPoint(startEl, range.startContainer, range.startOffset)
  const e = offsetAtPoint(startEl, range.endContainer, range.endOffset)
  if (s !== null && e !== null && e > s) {
    const shrunk = shrink(raw, s, e)
    if (text.slice(shrunk.start, shrunk.end) === shrunk.surface) {
      // 校验通过之后再吸附：先把 DOM 算出来的偏移验一遍，再决定要不要扩到整词
      const hit = snapToWholeWord(text, shrunk.start, shrunk.end, shrunk.surface)
      return { blockId, ...hit, kind: inferKind(hit.surface), exact: true }
    }
    // 端点偏移对但切片不符：多半是块内有额外文本（如列表符号），走路线 2
  }

  // ---- 路线 2：在正文里反查这段文字 ----
  const needle = raw.trim()
  const at = text.indexOf(needle)
  if (at >= 0 && text.indexOf(needle, at + 1) === -1) {
    const hit = snapToWholeWord(text, at, at + needle.length, needle)
    return {
      blockId,
      ...hit,
      kind: inferKind(hit.surface),
      exact: false,
    }
  }

  return null
}

/**
 * `readSelection` 的反函数：按块内字符偏移把位置还原成视口矩形。
 *
 * 用途是让浮层跟着正文走。浮层是 `position: fixed`，一滚动就会跟文字脱开。
 * 最初的做法是「一滚就关」，但划完词后页面常会自己动一下（正文里十几张
 * 图片陆续加载把排版撑开，浏览器滚动锚定随之调整 `scrollY`），浮层会当场
 * 闪没、用户根本点不到 —— 这个坑只有真浏览器能测出来。
 * 现在改成每次滚动都按偏移重新量一次位置，浮层永远贴着那行字。
 *
 * 走的是和 `readSelection` 同一套「拼接文本节点再定位」的办法，
 * 所以块里已经有 `<mark>` 把句子切成多个文本节点也能算对。
 */
export function rectOfOffsets(
  blockEl: HTMLElement,
  start: number,
  end: number,
): DOMRect | null {
  const walker = document.createTreeWalker(blockEl, NodeFilter.SHOW_TEXT)
  const nodes: { node: Text; start: number }[] = []
  let full = ''
  let n = walker.nextNode()
  while (n) {
    nodes.push({ node: n as Text, start: full.length })
    full += n.textContent ?? ''
    n = walker.nextNode()
  }
  if (nodes.length === 0) return null

  const locate = (off: number) => {
    for (const it of nodes) {
      const len = it.node.textContent?.length ?? 0
      if (off <= it.start + len) {
        return { node: it.node, offset: Math.max(0, off - it.start) }
      }
    }
    const last = nodes[nodes.length - 1]
    return { node: last.node, offset: last.node.textContent?.length ?? 0 }
  }

  const s = locate(start)
  const e = locate(end)
  const range = document.createRange()
  try {
    range.setStart(s.node, s.offset)
    range.setEnd(e.node, e.offset)
  } catch {
    return null // 节点已被替换（重渲染），交给调用方关闭浮层
  }
  const rect = range.getBoundingClientRect()
  if (rect.width === 0 && rect.height === 0) return null
  return rect
}

// ---------------------------------------------------------------- 高亮切分

export interface Segment {
  text: string
  /** null = 未高亮区间 */
  highlight: Highlight | null
}

/**
 * 把块文本按高亮区间切成片段，供渲染层重建高亮。
 *
 * 旧实现是游标式扫描：按起点排序后一路往后推，已被前一条覆盖的部分直接丢掉。
 * 无重叠时没问题，但**嵌套**时会出事 —— M4 实测踩到：从检索结果跳回原文时，
 * 命中区间落在一条更长的痕迹内部（比如搜一个词，那个词正好在某段已划线的
 * 句子里），游标走到那条长痕迹时已经把后面全吃掉了，定位标记一个字都渲染
 * 不出来，「闪一下」这个功能在最有用的场景下恰好失效。
 *
 * 现在改成按所有边界点切段、每段归属「覆盖它的、最该赢的那一条」：
 *   1. **负数 id 的定位标记优先** —— 那是用户此刻在找的位置，必须看得见；
 *   2. **区间短的优先** —— 嵌套时内层才是更具体的那条：长句里划的那个词必须
 *      看得见自己的底色（单词与句/短语是两种颜色，见 `index.css` 的
 *      `mark[data-kind='word']`）。旧规则「起点早的优先」会让整条长句把里面的
 *      词整段吃掉 —— 那正是用户分不清「哪个词查过译」的原因；
 *   3. 区间等长时起点早的优先（与旧规则一致，两条互不包含时结果不变）。
 * 无重叠场景的渲染结果与从前逐字相同，相邻同归属的段会并回去，
 * 不会把一个高亮打成多个 <mark>。
 */
export function splitByHighlights(text: string, hs: Highlight[]): Segment[] {
  const live = hs.filter((h) => h.end_offset > h.start_offset)
  if (!live.length) return [{ text, highlight: null }]

  const clamp = (n: number) => Math.max(0, Math.min(n, text.length))
  const cuts = new Set<number>([0, text.length])
  for (const h of live) {
    cuts.add(clamp(h.start_offset))
    cuts.add(clamp(h.end_offset))
  }
  const pts = [...cuts].sort((a, b) => a - b)

  const rank = (h: Highlight): [number, number, number] => [
    h.id < 0 ? 0 : 1,
    h.end_offset - h.start_offset,
    h.start_offset,
  ]
  const better = (a: Highlight, b: Highlight | null): boolean => {
    if (!b) return true
    const ka = rank(a)
    const kb = rank(b)
    for (let i = 0; i < 3; i += 1) if (ka[i] !== kb[i]) return ka[i] < kb[i]
    return false
  }

  const out: Segment[] = []
  for (let i = 0; i < pts.length - 1; i += 1) {
    const a = pts[i]
    const b = pts[i + 1]
    if (b <= a) continue

    let pick: Highlight | null = null
    for (const h of live) {
      if (clamp(h.start_offset) <= a && clamp(h.end_offset) >= b && better(h, pick)) {
        pick = h
      }
    }
    const prev = out[out.length - 1]
    if (prev && prev.highlight === pick) prev.text += text.slice(a, b)
    else out.push({ text: text.slice(a, b), highlight: pick })
  }
  return out.length ? out : [{ text, highlight: null }]
}

/** 判断某个块里是否有痕迹（用于决定是否渲染高亮层）。 */
export function hasHighlight(block: Block, byBlock: Map<number, Highlight[]>): boolean {
  return (byBlock.get(block.id)?.length ?? 0) > 0
}

/** 按 block_id 归拢痕迹，避免渲染时对每块都做一次线性查找。 */
export function groupByBlock(hs: Highlight[]): Map<number, Highlight[]> {
  const map = new Map<number, Highlight[]>()
  for (const h of hs) {
    const arr = map.get(h.block_id)
    if (arr) arr.push(h)
    else map.set(h.block_id, [h])
  }
  return map
}
