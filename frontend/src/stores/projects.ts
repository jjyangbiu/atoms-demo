import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api } from '@/api/client'

export interface ProjectOut {
  id: number
  name: string
  mode: 'engineer' | 'team'
  created_at: string
  updated_at: string
  published_slug: string | null
}

export interface PublishOut {
  slug: string
  url: string
}

export interface MessageOut {
  id: number
  // clarifier 为需求澄清智能体（工单 0015）
  role: 'user' | 'pm' | 'engineer' | 'clarifier' | 'system'
  // thinking 为推理模型的思考过程留痕，回看时以折叠块展示（诊断修复）
  // consensus/consensus_confirm 为需求澄清与共识确认（工单 0015）
  kind: 'text' | 'prd' | 'prd_confirm' | 'consensus' | 'consensus_confirm' | 'event' | 'thinking'
  content: string
  created_at: string
}

export interface FileOut {
  path: string
  size: number
}

export interface SnapshotOut {
  id: number
  rev: number
  file_count: number
  created_at: string
}

export const useProjectStore = defineStore('projects', () => {
  const projects = ref<ProjectOut[]>([])

  async function fetchProjects(): Promise<void> {
    projects.value = await api<ProjectOut[]>('/api/projects')
  }

  async function createProject(name: string, mode: 'engineer' | 'team'): Promise<ProjectOut> {
    const project = await api<ProjectOut>('/api/projects', {
      body: { name, mode },
    })
    await fetchProjects()
    return project
  }

  async function deleteProject(id: number): Promise<void> {
    await api(`/api/projects/${id}`, { method: 'DELETE' })
    await fetchProjects()
  }

  async function fetchMessages(projectId: number): Promise<MessageOut[]> {
    return api<MessageOut[]>(`/api/projects/${projectId}/messages`)
  }

  async function fetchFiles(projectId: number): Promise<FileOut[]> {
    return api<FileOut[]>(`/api/projects/${projectId}/files`)
  }

  async function fetchSnapshots(projectId: number): Promise<SnapshotOut[]> {
    return api<SnapshotOut[]>(`/api/projects/${projectId}/snapshots`)
  }

  async function rollbackSnapshot(projectId: number, snapshotId: number): Promise<SnapshotOut> {
    return api<SnapshotOut>(`/api/projects/${projectId}/snapshots/${snapshotId}/rollback`, {
      method: 'POST',
    })
  }

  async function publishProject(projectId: number): Promise<PublishOut> {
    return api<PublishOut>(`/api/projects/${projectId}/publish`, { method: 'POST' })
  }

  async function unpublishProject(projectId: number): Promise<void> {
    await api<void>(`/api/projects/${projectId}/publish`, { method: 'DELETE' })
  }

  return {
    projects,
    fetchProjects,
    createProject,
    deleteProject,
    fetchMessages,
    fetchFiles,
    fetchSnapshots,
    rollbackSnapshot,
    publishProject,
    unpublishProject,
  }
})
