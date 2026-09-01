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
  // clarifier 为需求澄清智能体（工单 0015），spec_agent 为需求规格智能体（工单 0016），
  // breaker_agent 为拆单智能体（工单 0017）
  role: 'user' | 'pm' | 'engineer' | 'clarifier' | 'spec_agent' | 'breaker_agent' | 'system'
  // thinking 为推理模型的思考过程留痕，回看时以折叠块展示（诊断修复）
  // clarify 为选项式澄清问题卡片（内容为问题清单 JSON，诊断修复）
  // clarify_answer 为弹窗式澄清的答案消息标记，前端折叠进问答记录卡（工单 0020）
  // consensus/consensus_confirm 为需求澄清与共识确认（工单 0015）
  // spec/spec_confirm 为需求规格与规格确认（工单 0016）
  // tickets/tickets_confirm 为工单清单与清单确认（工单 0017）
  // ticket 为单张工单的执行进度行（工单 0018）
  kind:
    | 'text'
    | 'prd'
    | 'prd_confirm'
    | 'consensus'
    | 'consensus_confirm'
    | 'spec'
    | 'spec_confirm'
    | 'tickets'
    | 'tickets_confirm'
    | 'ticket'
    | 'event'
    | 'thinking'
    | 'clarify'
    | 'clarify_answer'
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
  // 形成该检查点的工单序号（工单 0019）；非检查点快照为 null
  ticket_seq?: number | null
}

// 工单清单条目（工单 0017）：执行状态与检查点快照版本由串行执行写入（工单 0018）
export interface TicketOut {
  seq: number
  title: string
  deliverable: string
  blocked_by: number[]
  status: 'open' | 'running' | 'done' | 'failed'
  snapshot_rev: number | null
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

  async function fetchTickets(projectId: number): Promise<TicketOut[]> {
    return api<TicketOut[]>(`/api/projects/${projectId}/tickets`)
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
    fetchTickets,
    rollbackSnapshot,
    publishProject,
    unpublishProject,
  }
})
