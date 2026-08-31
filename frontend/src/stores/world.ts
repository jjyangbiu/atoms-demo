import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api } from '@/api/client'
import type { ProjectOut } from '@/stores/projects'

/** App 世界画廊条目（工单 0008）：已发布应用的公开卡片信息 */
export interface WorldAppOut {
  slug: string
  title: string
  description: string
  author: string
  preview_url: string
  published_at: string
}

export const useWorldStore = defineStore('world', () => {
  const apps = ref<WorldAppOut[]>([])

  async function fetchWorld(): Promise<void> {
    apps.value = await api<WorldAppOut[]>('/api/world')
  }

  async function fetchWorldApp(slug: string): Promise<WorldAppOut> {
    return api<WorldAppOut>(`/api/world/${slug}`)
  }

  /** 克隆已发布应用到当前用户名下，返回新项目 */
  async function cloneApp(slug: string): Promise<ProjectOut> {
    return api<ProjectOut>(`/api/world/${slug}/clone`, { method: 'POST' })
  }

  return { apps, fetchWorld, fetchWorldApp, cloneApp }
})
