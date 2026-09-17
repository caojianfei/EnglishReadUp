/**
 * SSE 解析（M5）。
 *
 * **不用 `EventSource`**：它只能发 GET 且带不了 body，而我们的 AI 请求要带
 * 一大段原文。所以用 `fetch` + `ReadableStream` 自己读，手动切帧 ——
 * 协议本身很简单（`event:` / `data:` 行 + 空行结尾），比引一个库划算。
 *
 * 两条服务端契约（见 backend/app/api/ai.py 的 `_frame`）：
 *   - 每个帧以空行结束，所以按 `\n\n` 切段是安全的；
 *   - `data:` 后面的内容一定是单行合法 JSON（服务端用 ensure_ascii=False +
 *     json.dumps，模型输出里的换行已经被转义成 `\n`），所以不必处理跨行 data。
 *
 * 心跳是 SSE 注释帧（`: ping`），按协议忽略即可。
 */

import type { AiResponse, AiTaskType } from './types'

export interface AiStreamPayload {
  kind: AiTaskType
  article_id: number
  text: string
  block_id?: number | null
  context?: string | null
  highlight_id?: number | null
  word?: string | null
  sentence?: string | null
  hint?: string
  force?: boolean
}

export interface StreamHandlers {
  /** 连接建立。附 prompt_version，便于排查「缓存是新是旧」 */
  onOpen?: (info: { kind: string; prompt_version: string }) => void
  /**
   * 正文增量。**`text` 为空串表示上游在思考、还没有正文** ——
   * 用来把「一动不动」改成「模型在想」，而不是真的往屏幕上加字符。
   */
  onDelta?: (text: string) => void
  /** 上游重试了，请清空已显示的增量，否则两次输出会首尾相接 */
  onReset?: () => void
  onDone: (resp: AiResponse) => void
  onError: (status: number, detail: string) => void
}

/** 从 FastAPI 的错误体里抠出 `detail`，抠不到就原样返回。 */
export function detailOf(body: string, fallback = ''): string {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown }
    if (typeof parsed.detail === 'string') return parsed.detail
    if (Array.isArray(parsed.detail)) {
      const first = parsed.detail[0] as { msg?: string } | undefined
      if (first?.msg) return first.msg
    }
  } catch {
    /* 不是 JSON */
  }
  return body.slice(0, 200) || fallback
}

type Frame = { event: string; data: unknown }

function parseFrame(raw: string): Frame | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of raw.split('\n')) {
    if (line.startsWith(':')) continue // 注释/心跳
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (dataLines.length === 0) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) as unknown }
  } catch {
    return null // 半截 JSON：丢掉而不是让整次调用失败
  }
}

/**
 * 发起一次流式 AI 调用。**正常结束/失败都通过 handler 汇报**，
 * 只有网络层异常才 throw —— 调用方对两者的处理不一样（前者进面板，
 * 后者是「连不上后端」）。
 */
export async function streamAi(
  payload: AiStreamPayload,
  h: StreamHandlers,
): Promise<void> {
  const res = await fetch('/api/ai/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
  })

  if (!res.ok || !res.body) {
    const body = await res.text().catch(() => '')
    h.onError(res.status, detailOf(body, res.statusText))
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let settled = false // 收到 done / error 就算交代完了

  const dispatch = (frame: Frame) => {
    switch (frame.event) {
      case 'open':
        h.onOpen?.(frame.data as { kind: string; prompt_version: string })
        break
      case 'delta':
        h.onDelta?.((frame.data as { text?: string })?.text ?? '')
        break
      case 'reset':
        h.onReset?.()
        break
      case 'done':
        settled = true
        h.onDone(frame.data as AiResponse)
        break
      case 'error': {
        settled = true
        const d = frame.data as { status?: number; detail?: string }
        h.onError(d?.status ?? 0, d?.detail ?? 'AI 调用失败')
        break
      }
      default:
        break // 未知事件名：向前兼容，忽略
    }
  }

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    // 有的中间层会把行尾改成 CRLF，先归一化再找帧分隔
    buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    for (;;) {
      const cut = buf.indexOf('\n\n')
      if (cut < 0) break
      const raw = buf.slice(0, cut)
      buf = buf.slice(cut + 2)
      const frame = parseFrame(raw)
      if (frame) dispatch(frame)
    }
  }

  // 连接断了却没给 done / error：不能默默返回，否则面板会一直转圈
  if (!settled) h.onError(0, '连接中断，未收到完整结果（可重试）')
}
