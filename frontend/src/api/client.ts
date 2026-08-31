/**
 * 极简 API 客户端：fetch 封装 + JWT 携带 + 401 统一跳转登录。
 * 生产环境由 nginx 同源提供 /api，开发环境经 Vite 代理。
 */

const TOKEN_KEY = 'atoms_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    /** 限流（429）时后端给出的建议重试等待秒数（工单 0011） */
    public retryAfter: number | null = null,
  ) {
    super(detail)
  }
}

/**
 * 解析错误响应体：兼容字符串 detail 与结构化 detail（限流 429：
 * { error, reason, retry_after, message }，工单 0011）。
 */
export function extractError(
  statusText: string,
  data: unknown,
): { detail: string; retryAfter: number | null } {
  let detail = statusText
  let retryAfter: number | null = null
  const d = (data as { detail?: unknown } | null)?.detail
  if (typeof d === 'string') {
    detail = d
  } else if (d && typeof d === 'object') {
    const obj = d as Record<string, unknown>
    if (typeof obj.message === 'string') detail = obj.message
    if (typeof obj.retry_after === 'number') retryAfter = obj.retry_after
  }
  return { detail, retryAfter }
}

interface ApiOptions {
  method?: string
  body?: unknown
  /** 401 时是否静默（登录/注册页不跳转） */
  silent401?: boolean
}

export async function api<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  const resp = await fetch(path, {
    method: options.method ?? (options.body !== undefined ? 'POST' : 'GET'),
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  })

  if (resp.status === 401 && !options.silent401 && token) {
    clearToken()
    window.location.href = '/login'
    throw new ApiError(401, '登录已过期，请重新登录')
  }

  if (!resp.ok) {
    let data: unknown = null
    try {
      data = await resp.json()
    } catch {
      /* 非 JSON 错误体，保留 statusText */
    }
    const { detail, retryAfter } = extractError(resp.statusText, data)
    throw new ApiError(resp.status, detail, retryAfter)
  }

  // 204 等无响应体的成功状态（如 DELETE）不能解析 JSON
  if (resp.status === 204) return undefined as T

  return (await resp.json()) as T
}
