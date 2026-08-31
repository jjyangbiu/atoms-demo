/**
 * SSE 消费：fetch + ReadableStream（EventSource 无法携带 Authorization，故不用）。
 * 事件协议与后端约定一致：每行 `data: {json}`，事件含 type 字段
 * （text | thinking | tool | done | error；团队模式历史项目另有 prd 增量事件（工单 0010），
 * 新团队项目需求规格阶段为 spec 增量事件，工单 0016）。
 * thinking 为推理模型的思考过程增量，前端以小号可折叠块展示。
 */

import { ApiError, extractError, getToken } from '@/api/client'

export interface SseEvent {
  type: 'text' | 'thinking' | 'tool' | 'done' | 'error' | 'prd' | 'consensus' | 'spec' | string
  [key: string]: unknown
}

export async function streamPost(
  path: string,
  body: unknown,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const resp = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) })
  if (!resp.ok || !resp.body) {
    let data: unknown = null
    try {
      data = await resp.json()
    } catch {
      /* 非 JSON 错误体 */
    }
    const { detail, retryAfter } = extractError(resp.statusText, data)
    throw new ApiError(resp.status, detail, retryAfter)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      for (const line of frame.split('\n')) {
        if (line.startsWith('data: ')) {
          onEvent(JSON.parse(line.slice(6)) as SseEvent)
        }
      }
    }
  }
}
