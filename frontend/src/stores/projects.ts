import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api } from '@/api/client'

export interface ProjectOut {
  id: number
  name: string
  mode: 'engineer' | 'team'
  created_at: string
  updated_at: string
}

export interface MessageOut {
  id: number
  role: 'user' | 'pm' | 'engineer' | 'system'
  kind: 'text' | 'prd' | 'event'
  content: string
  created_at: string
}

export interface FileOut {
  path: string
  size: number
}

export const useProjectStore = defineStore('projects', () => {
  const projects = ref<ProjectOut[]>([])

  async function fetchProjects(): Promise<void> {
    projects.value = await api<ProjectOut[]>('/api/projects')
  }

  async function createProject(name: string): Promise<ProjectOut> {
    const project = await api<ProjectOut>('/api/projects', {
      body: { name, mode: 'engineer' },
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

  return { projects, fetchProjects, createProject, deleteProject, fetchMessages, fetchFiles }
})
