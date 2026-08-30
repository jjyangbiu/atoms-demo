<script setup lang="ts">
import { marked } from 'marked'
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '@/api/client'
import { streamPost, type SseEvent } from '@/api/sse'
import { useProjectStore, type MessageOut } from '@/stores/projects'

interface ToolInfo {
  name: string
  args: Record<string, unknown>
  status: 'start' | 'done' | 'error'
  result?: string
}

interface ChatEntry {
  id: string
  kind: 'user' | 'text' | 'tool'
  content: string
  tool?: ToolInfo
  streaming?: boolean
}

const route = useRoute()
const router = useRouter()
const store = useProjectStore()

const projectId = computed(() => Number(route.params.id))
const projectName = ref('')
const entries = ref<ChatEntry[]>([])
const input = ref('')
const generating = ref(false)
const errorDetail = ref('')
const chatBodyRef = ref<HTMLElement | null>(null)
let seq = 0
let lastUserContent = ''

function nextId() {
  return `local-${++seq}`
}

const isEmpty = computed(
  () => entries.value.length === 0 && !generating.value,
)

function renderMarkdown(content: string) {
  return marked.parse(content, { async: false }) as string
}

function toolLabel(tool: ToolInfo): string {
  const verb = { write_file: '写入', edit_file: '修改', read_file: '读取' }[tool.name] ?? tool.name
  const path = typeof tool.args.path === 'string' ? tool.args.path : ''
  return `${verb} ${path}`
}

function scrollToBottom() {
  nextTick(() => {
    chatBodyRef.value?.scrollTo({ top: chatBodyRef.value.scrollHeight })
  })
}

function toEntries(messages: MessageOut[]): ChatEntry[] {
  const result: ChatEntry[] = []
  for (const m of messages) {
    if (m.kind === 'event') {
      try {
        const tool = JSON.parse(m.content) as ToolInfo
        result.push({ id: `msg-${m.id}`, kind: 'tool', content: '', tool })
      } catch {
        /* 事件行解析失败则跳过 */
      }
    } else if (m.kind === 'text') {
      result.push({
        id: `msg-${m.id}`,
        kind: m.role === 'user' ? 'user' : 'text',
        content: m.content,
      })
    }
  }
  return result
}

async function loadProject() {
  const project = await api<{ name: string }>(`/api/projects/${projectId.value}`)
  projectName.value = project.name
}

async function loadHistory() {
  const messages = await store.fetchMessages(projectId.value)
  entries.value = toEntries(messages)
  scrollToBottom()
}

onMounted(async () => {
  await loadProject()
  await loadHistory()
})

async function send() {
  const content = input.value.trim()
  if (!content || generating.value) return
  input.value = ''
  entries.value.push({ id: nextId(), kind: 'user', content })
  lastUserContent = content
  await generate(content)
}

function retry() {
  if (!lastUserContent || generating.value) return
  void generate(lastUserContent)
}

async function generate(content: string) {
  generating.value = true
  errorDetail.value = ''
  const textHolder: { entry: ChatEntry | null } = { entry: null }

  const ensureTextEntry = (): ChatEntry => {
    if (!textHolder.entry) {
      textHolder.entry = { id: nextId(), kind: 'text', content: '', streaming: true }
      entries.value.push(textHolder.entry)
    }
    return textHolder.entry
  }

  try {
    await streamPost(`/api/projects/${projectId.value}/messages`, { content }, (event: SseEvent) => {
      if (event.type === 'text') {
        ensureTextEntry().content += String(event.content ?? '')
      } else if (event.type === 'tool') {
        const tool = {
          name: String(event.name ?? ''),
          args: (event.args as Record<string, unknown>) ?? {},
          status: (event.status as ToolInfo['status']) ?? 'done',
          result: event.result as string | undefined,
        }
        if (tool.status === 'start') {
          entries.value.push({ id: nextId(), kind: 'tool', content: '', tool })
        } else {
          // 收尾事件并入对应的 start 卡片，避免重复
          const match = [...entries.value]
            .reverse()
            .find(
              (e) =>
                e.kind === 'tool' &&
                e.tool?.name === tool.name &&
                e.tool.args.path === tool.args.path &&
                e.tool.status === 'start',
            )
          if (match?.tool) match.tool = { ...tool }
          else entries.value.push({ id: nextId(), kind: 'tool', content: '', tool })
        }
      } else if (event.type === 'error') {
        errorDetail.value = String(event.detail ?? '生成失败')
      }
      scrollToBottom()
    })
  } catch (e) {
    errorDetail.value = e instanceof Error ? e.message : '请求失败'
  } finally {
    generating.value = false
    if (textHolder.entry) textHolder.entry.streaming = false
    await loadHistory() // 以服务端持久化结果为准对齐
    scrollToBottom()
  }
}
</script>

<template>
  <div class="project-page">
    <header class="topbar">
      <el-button text @click="router.push('/workspace')">← 返回</el-button>
      <span class="project-title">{{ projectName }}</span>
      <el-tag size="small" type="info">工程师模式</el-tag>
    </header>

    <div class="body">
      <section class="chat-panel">
        <div ref="chatBodyRef" class="chat-body">
          <el-empty
            v-if="isEmpty"
            description="描述你想构建的应用，例如：做一个番茄钟，带统计功能"
          />
          <template v-for="entry in entries" :key="entry.id">
            <div v-if="entry.kind === 'user'" class="msg user-msg">
              <div class="bubble user-bubble">{{ entry.content }}</div>
            </div>
            <div v-else-if="entry.kind === 'tool'" class="tool-line">
              <el-tag
                :type="entry.tool?.status === 'error' ? 'danger' : 'success'"
                size="small"
                effect="plain"
              >
                <span v-if="entry.tool?.status === 'start'" class="tool-running">⚙ {{ toolLabel(entry.tool) }}…</span>
                <span v-else>✓ {{ entry.tool ? toolLabel(entry.tool) : '' }}</span>
              </el-tag>
              <span v-if="entry.tool?.status === 'error'" class="tool-error-text">
                {{ entry.tool.result }}
              </span>
            </div>
            <div v-else class="msg agent-msg">
              <div class="bubble agent-bubble markdown" v-html="renderMarkdown(entry.content || (entry.streaming ? '思考中…' : ''))" />
            </div>
          </template>
          <div v-if="generating" class="generating-hint">智能体正在工作…</div>
          <div v-else-if="errorDetail" class="error-banner" data-testid="chat-error">
            <span>⚠ {{ errorDetail }}</span>
            <el-button type="danger" size="small" data-testid="chat-retry" @click="retry">
              重新生成
            </el-button>
          </div>
        </div>

        <div class="chat-input">
          <el-input
            v-model="input"
            type="textarea"
            :rows="3"
            :disabled="generating"
            placeholder="描述需求或提出修改，例如：把按钮改大一点"
            data-testid="chat-input"
            @keydown.enter.exact.prevent="send"
          />
          <el-button
            type="primary"
            :loading="generating"
            :disabled="!input.trim()"
            data-testid="chat-send"
            @click="send"
          >
            {{ generating ? '生成中' : '发送' }}
          </el-button>
        </div>
      </section>

      <section class="preview-panel">
        <el-empty description="预览区：生成后的应用将在这里实时运行（下个工单交付）" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.project-page {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.project-title {
  font-size: 16px;
  font-weight: 600;
}

.body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.chat-panel {
  width: 42%;
  min-width: 380px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e4e7ed;
  background: #fff;
}

.chat-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.msg {
  display: flex;
}

.user-msg {
  justify-content: flex-end;
}

.bubble {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.6;
}

.user-bubble {
  background: #409eff;
  color: #fff;
  white-space: pre-wrap;
}

.agent-bubble {
  background: #f4f4f5;
  color: #303133;
}

.markdown :deep(p) {
  margin: 0 0 8px;
}

.markdown :deep(p:last-child) {
  margin-bottom: 0;
}

.markdown :deep(code) {
  background: #e9e9eb;
  padding: 1px 5px;
  border-radius: 4px;
}

.tool-line {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 4px;
}

.tool-running {
  opacity: 0.8;
}

.tool-error-text {
  color: #f56c6c;
  font-size: 12px;
}

.generating-hint {
  color: #909399;
  font-size: 12px;
  padding-left: 4px;
}

.error-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border-radius: 8px;
  background: #fef0f0;
  color: #f56c6c;
  font-size: 13px;
}

.chat-input {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  padding: 12px 16px;
  border-top: 1px solid #e4e7ed;
}

.chat-input .el-button {
  height: 48px;
}

.preview-panel {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fafafa;
}
</style>
