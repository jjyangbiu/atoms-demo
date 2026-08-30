import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, api, clearToken, getToken, setToken } from '@/api/client'
import router from '@/router'

export interface UserOut {
  id: number
  username: string
  created_at: string
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserOut | null>(null)
  const isAuthenticated = computed(() => user.value !== null)

  async function register(username: string, password: string): Promise<void> {
    await api<UserOut>('/api/auth/register', { body: { username, password }, silent401: true })
    await login(username, password)
  }

  async function login(username: string, password: string): Promise<void> {
    const { access_token } = await api<{ access_token: string }>('/api/auth/login', {
      body: { username, password },
      silent401: true,
    })
    setToken(access_token)
    await fetchMe()
  }

  async function fetchMe(): Promise<void> {
    if (!getToken()) return
    try {
      user.value = await api<UserOut>('/api/auth/me', { silent401: true })
    } catch (e) {
      user.value = null
      if (e instanceof ApiError && e.status === 401) {
        // 令牌失效/过期：清除并引导重新登录（用户故事 5）
        clearToken()
        router.push({ name: 'login' })
      }
    }
  }

  async function logout(): Promise<void> {
    try {
      await api('/api/auth/logout', { method: 'POST', silent401: true })
    } finally {
      clearToken()
      user.value = null
    }
  }

  return { user, isAuthenticated, register, login, fetchMe, logout }
})
