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
  ) {
    super(detail)
  }
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
    let detail = resp.statusText
    try {
      const data = await resp.json()
      if (typeof data?.detail === 'string') detail = data.detail
    } catch {
      /* 非 JSON 错误体，保留 statusText */
    }
    throw new ApiError(resp.status, detail)
  }

  return (await resp.json()) as T
}
