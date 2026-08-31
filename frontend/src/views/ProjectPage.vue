<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, ApiError } from '@/api/client'
import { streamPost, type SseEvent } from '@/api/sse'
import { useProjectStore, type FileOut, type MessageOut, type SnapshotOut } from '@/stores/projects'

// 代码视图异步加载：Monaco 体积较大，仅在切到代码 Tab 时才拉取（工单 0007）
const CodeView = defineAsyncComponent(() => import('@/components/CodeView.vue'))

interface ToolInfo {
  name: string
  args: Record<string, unknown>
  status: 'start' | 'done' | 'error'
  result?: string
}

// 工单清单卡片字段（工单 0017）：标题 / 交付内容 / 被谁阻塞 / 状态；
// seq 为清单内相对序号（与 blocked_by 引用同口径）
interface TicketInfo {
  seq: number
  title: string
  deliverable: string
  blocked_by: number[]
}

interface ChatEntry {
  id: string
  kind: 'user' | 'text' | 'tool' | 'prd' | 'consensus' | 'spec' | 'tickets' | 'thinking'
  // content 为打字机当前已显现的文本；raw 为已收到的完整增量（打字机源）
  content: string
  raw?: string
  tool?: ToolInfo
  streaming?: boolean
  // 思考块是否折叠：流式过程默认展开，结束后默认折叠（诊断修复）
  collapsed?: boolean
  // PRD 卡片（工单 0010，仅历史团队项目）：已确认的不再显示操作区，仅待确认的卡片可交互；
  // 未确认前发送普通消息会被后端引导先处理 PRD（工单 0010）
  prdConfirmed?: boolean
  // 需求共识卡片（工单 0015）：交互规则同 PRD 卡片；待确认时发消息会重新澄清
  consensusConfirmed?: boolean
  // 需求规格卡片（工单 0016，团队模式）：交互规则同共识卡片；待确认时发消息会重新起草规格
  specConfirmed?: boolean
  // 工单清单卡片（工单 0017，团队模式）：交互规则同规格卡片；待确认时发消息会重新拆解，
  // 确认后进入执行期，不再重新澄清/拆单
  tickets?: TicketInfo[]
  ticketsConfirmed?: boolean
}

function parseTickets(content: string): TicketInfo[] {
  try {
    const data = JSON.parse(content)
    if (!Array.isArray(data)) return []
    return data.map((t: Record<string, unknown>, i: number) => ({
      seq: i + 1,
      title: String(t.title ?? ''),
      deliverable: String(t.deliverable ?? ''),
      blocked_by: Array.isArray(t.blocked_by) ? t.blocked_by.map(Number) : [],
    }))
  } catch {
    return []
  }
}

const route = useRoute()
const router = useRouter()
const store = useProjectStore()

const projectId = computed(() => Number(route.params.id))
const projectName = ref('')
const projectMode = ref<'engineer' | 'team'>('engineer')
const entries = ref<ChatEntry[]>([])
const input = ref('')
const generating = ref(false)
const errorDetail = ref('')
// 刷新/断流导致上一轮生成被中断（诊断修复）：后端已把流出的思考落库，
// 加载历史时据"消息尾不是收尾结论"识别并展示重试入口，避免用户以为状态丢失
type InterruptedState = { status: 'unknown' | 'none' | 'interrupted' }
const interrupted = ref<InterruptedState>({ status: 'unknown' })
// PRD 确认时的追加意见（工单 0010）；同一时刻至多一张待确认卡片，单值即可
const prdFeedback = ref('')
// 需求共识确认时的修改意见（工单 0015）
const consensusFeedback = ref('')
// 需求规格确认时的修改意见（工单 0016）
const specFeedback = ref('')
// 工单清单确认时的调整意见（工单 0017）
const ticketsFeedback = ref('')
const chatBodyRef = ref<HTMLElement | null>(null)
const hasIndex = ref(false)
const previewRev = ref(0)
const publishedSlug = ref<string | null>(null)
const publishing = ref(false)
// 右侧面板双 Tab：预览 / 代码（工单 0007）；版本历史以抽屉展示
const activeTab = ref<'preview' | 'code'>('preview')
const fileList = ref<FileOut[]>([])
const snapshots = ref<SnapshotOut[]>([])
const historyVisible = ref(false)
const rollingBackId = ref<number | null>(null)
let seq = 0
let lastUserContent = ''

function nextId() {
  return `local-${++seq}`
}

const isEmpty = computed(
  () => entries.value.length === 0 && !generating.value,
)

// rev 参数在每次生成/迭代完成后递增，强制 iframe 刷新到最新版本（工单 0005）；
// 预览鉴权靠登录 Cookie 自动携带，无需在 URL 里暴露令牌
const previewSrc = computed(() =>
  hasIndex.value
    ? `/api/projects/${projectId.value}/preview/index.html?rev=${previewRev.value}`
    : '',
)

// 发布状态（工单 0006）：稳定公开链接 /p/{slug}，任何人无需登录可访问；
// 迭代成功后后端直接提供项目目录当前文件，链接内容自动同步而链接本身不变
const publicUrl = computed(() =>
  publishedSlug.value ? `${window.location.origin}/p/${publishedSlug.value}` : '',
)

function renderMarkdown(content: string) {
  return marked.parse(content, { async: false }) as string
}

function toolLabel(tool: ToolInfo): string {
  if (tool.name === 'search_templates') {
    // 模板检索事件（工单 0009）：展示检索词而非文件路径
    const query = typeof tool.args.query === 'string' ? tool.args.query : ''
    return `检索模板 ${query}`
  }
  const verb = { write_file: '写入', edit_file: '修改', read_file: '读取' }[tool.name] ?? tool.name
  const path = typeof tool.args.path === 'string' ? tool.args.path : ''
  return `${verb} ${path}`
}

function scrollToBottom() {
  nextTick(() => {
    chatBodyRef.value?.scrollTo({ top: chatBodyRef.value.scrollHeight })
  })
}

// 打字机（诊断修复）：服务端增量先入 entry.raw，定时器逐拍把字符搬进 content；
// 积压越大搬运越快，保证流结束后短时间内追平，历史回看则整段直出不重放
let typewriterTimer: number | null = null

function pushText(entry: ChatEntry, piece: string) {
  entry.raw = (entry.raw ?? '') + piece
  if (typewriterTimer === null) {
    typewriterTimer = window.setInterval(typewriterTick, 24)
  }
}

function typewriterTick() {
  let active = false
  for (const entry of entries.value) {
    const raw = entry.raw ?? ''
    if (entry.content.length >= raw.length) continue
    active = true
    const backlog = raw.length - entry.content.length
    entry.content = raw.slice(0, entry.content.length + Math.max(2, Math.ceil(backlog / 25)))
  }
  if (active) scrollToBottom()
  if (!active && typewriterTimer !== null) {
    window.clearInterval(typewriterTimer)
    typewriterTimer = null
  }
}

function waitForTypewriter(timeoutMs = 1500): Promise<void> {
  // 收尾前等打字机追平，避免刷新历史时尾巴上的字突然闪现；超时兜底不阻塞
  return new Promise((resolve) => {
    const start = Date.now()
    const check = () => {
      const pending = entries.value.some((e) => e.content.length < (e.raw ?? '').length)
      if (!pending || Date.now() - start > timeoutMs) resolve()
      else window.setTimeout(check, 30)
    }
    check()
  })
}

onBeforeUnmount(() => {
  if (typewriterTimer !== null) {
    window.clearInterval(typewriterTimer)
    typewriterTimer = null
  }
})

function toEntries(messages: MessageOut[]): ChatEntry[] {
  const result: ChatEntry[] = []
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i]
    if (m.kind === 'event') {
      try {
        const tool = JSON.parse(m.content) as ToolInfo
        result.push({ id: `msg-${m.id}`, kind: 'tool', content: '', tool })
      } catch {
        /* 事件行解析失败则跳过 */
      }
    } else if (m.kind === 'prd') {
      // 其后存在确认消息即视为已确认（工单 0010）
      const confirmed = messages.slice(i + 1).some((x) => x.kind === 'prd_confirm')
      result.push({ id: `msg-${m.id}`, kind: 'prd', content: m.content, prdConfirmed: confirmed })
    } else if (m.kind === 'consensus') {
      // 需求共识卡片（工单 0015）：确认状态推导同 PRD；仅最新一张待确认卡片可交互，
      // 旧卡片（被重新澄清取代的）即使未确认也不再显示操作区——下方确认按钮只认尾部
      const confirmed = messages.slice(i + 1).some((x) => x.kind === 'consensus_confirm')
      const superseded = messages.slice(i + 1).some((x) => x.kind === 'consensus')
      result.push({
        id: `msg-${m.id}`,
        kind: 'consensus',
        content: m.content,
        consensusConfirmed: confirmed || superseded,
      })
    } else if (m.kind === 'spec') {
      // 需求规格卡片（工单 0016）：确认状态推导同共识；被重新起草取代的旧卡片不再可交互
      const confirmed = messages.slice(i + 1).some((x) => x.kind === 'spec_confirm')
      const superseded = messages.slice(i + 1).some((x) => x.kind === 'spec')
      result.push({
        id: `msg-${m.id}`,
        kind: 'spec',
        content: m.content,
        specConfirmed: confirmed || superseded,
      })
    } else if (m.kind === 'tickets') {
      // 工单清单卡片（工单 0017）：确认状态推导同规格；被重新拆解取代的旧卡片不再可交互；
      // 刷新后由历史行重建，回看不丢（工单数据同时持久化于工单表）
      const confirmed = messages.slice(i + 1).some((x) => x.kind === 'tickets_confirm')
      const superseded = messages.slice(i + 1).some((x) => x.kind === 'tickets')
      result.push({
        id: `msg-${m.id}`,
        kind: 'tickets',
        content: m.content,
        tickets: parseTickets(m.content),
        ticketsConfirmed: confirmed || superseded,
      })
    } else if (m.kind === 'thinking') {
      // 思考历史回看：整段直出、默认折叠（诊断修复）
      result.push({ id: `msg-${m.id}`, kind: 'thinking', content: m.content, collapsed: true })
    } else if (m.kind === 'prd_confirm' || m.kind === 'consensus_confirm' || m.kind === 'spec_confirm' || m.kind === 'tickets_confirm') {
      // 确认（含追加意见）以用户消息呈现，回看时一目了然（工单 0010/0015/0016/0017）
      result.push({ id: `msg-${m.id}`, kind: 'user', content: m.content })
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
  const project = await api<{ name: string; mode: 'engineer' | 'team'; published_slug: string | null }>(
    `/api/projects/${projectId.value}`,
  )
  projectName.value = project.name
  projectMode.value = project.mode
  publishedSlug.value = project.published_slug
}

async function onPublish() {
  publishing.value = true
  try {
    const pub = await store.publishProject(projectId.value)
    publishedSlug.value = pub.slug
    ElMessage.success('发布成功，链接已生成')
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? e.detail : '发布失败')
  } finally {
    publishing.value = false
  }
}

async function onUnpublish() {
  try {
    await ElMessageBox.confirm('取消发布后，公开链接将立即失效。确定取消发布？', '取消发布', {
      type: 'warning',
    })
  } catch {
    return
  }
  await store.unpublishProject(projectId.value)
  publishedSlug.value = null
  ElMessage.success('已取消发布')
}

async function copyLink() {
  try {
    await navigator.clipboard.writeText(publicUrl.value)
    ElMessage.success('链接已复制')
  } catch {
    ElMessage.warning(`自动复制失败，请手动复制：${publicUrl.value}`)
  }
}

async function loadHistory() {
  const messages = await store.fetchMessages(projectId.value)
  entries.value = toEntries(messages)
  detectInterrupted(messages)
  scrollToBottom()
}

// 中断识别（诊断修复）：正常收尾的最后一轮必以结论（text/共识/规格）收尾；
// 消息尾停在思考行或工具事件行 = 该轮被刷新/断流中断。同时把最后一条用户消息
// 恢复为可重试内容（刷新后 lastUserContent 已丢失，重试按钮才可用）
function detectInterrupted(messages: MessageOut[]) {
  const last = messages[messages.length - 1]
  if (last && (last.kind === 'thinking' || last.kind === 'event')) {
    const lastUser = [...messages].reverse().find((m) => m.role === 'user')
    if (lastUser) {
      lastUserContent = lastUser.content
      interrupted.value = { status: 'interrupted' }
      return
    }
  }
  interrupted.value = { status: 'none' }
}

async function loadFiles() {
  fileList.value = await store.fetchFiles(projectId.value)
  hasIndex.value = fileList.value.some((f) => f.path === 'index.html')
}

async function loadSnapshots() {
  snapshots.value = await store.fetchSnapshots(projectId.value)
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString()
}

async function onRollback(snapshot: SnapshotOut) {
  try {
    await ElMessageBox.confirm(
      `回滚到版本 ${snapshot.rev} 后，当前文件将被替换为该版本状态，后续迭代以其为基线。确定回滚？`,
      '回滚版本',
      { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  rollingBackId.value = snapshot.id
  try {
    await store.rollbackSnapshot(projectId.value, snapshot.id)
    await Promise.all([loadFiles(), loadSnapshots()])
    previewRev.value += 1
    ElMessage.success(`已回滚到版本 ${snapshot.rev}`)
    historyVisible.value = false
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? e.detail : '回滚失败')
  } finally {
    rollingBackId.value = null
  }
}

onMounted(async () => {
  await loadProject()
  await Promise.all([loadHistory(), loadFiles(), loadSnapshots()])
})

async function send() {
  const content = input.value.trim()
  if (!content || generating.value) return
  input.value = ''
  entries.value.push({ id: nextId(), kind: 'user', content })
  lastUserContent = content
  const err = await runSse(`/api/projects/${projectId.value}/messages`, { content })
  if (err?.status === 429) {
    // 限流被拒不落库：把输入还给用户，稍后可直接重发（工单 0011）
    input.value = content
  }
}

function retry() {
  if (!lastUserContent || generating.value) return
  void runSse(`/api/projects/${projectId.value}/messages`, { content: lastUserContent })
}

// 确认 PRD（工单 0010）：确认后工程师智能体随即开始生成，后续迭代与工程师模式一致；
// 追加意见可选，随确认一并交给工程师，同步落对话历史可回看（工单 0010）
async function confirmPrd() {
  if (generating.value) return
  const feedback = prdFeedback.value.trim()
  prdFeedback.value = ''
  // 与后端持久化文案一致（刷新后回看不变）：有意见存意见，无意见存固定确认语（工单 0010）
  entries.value.push({
    id: nextId(),
    kind: 'user',
    content: feedback || '确认通过，开始实现。',
  })
  const err = await runSse(`/api/projects/${projectId.value}/prd/confirm`, { feedback })
  if (err?.status === 429) {
    // 限流被拒不落确认：还回追加意见，稍后可直接重新确认（工单 0011）
    prdFeedback.value = feedback
  }
}

// 确认需求共识（工单 0015）：工程师模式确认后随即生成；团队模式确认后规格智能体
// 开始起草需求规格（工单 0016）；修改意见可选，随确认一并落对话历史可回看；
// 首建流水线内确认不占用新名额，不会遇限流（ADR 0003）
async function confirmConsensus() {
  if (generating.value) return
  const feedback = consensusFeedback.value.trim()
  consensusFeedback.value = ''
  entries.value.push({
    id: nextId(),
    kind: 'user',
    content:
      feedback ||
      (projectMode.value === 'team' ? '确认共识，开始起草需求规格。' : '确认共识，开始生成。'),
  })
  await runSse(`/api/projects/${projectId.value}/consensus/confirm`, { feedback })
}

// 确认需求规格（工单 0016）：确认后拆单智能体随即开始拆解工单（工单 0017）；
// 修改意见可选，随确认一并落对话历史可回看；首建流水线内确认不占用新名额（ADR 0003）
async function confirmSpec() {
  if (generating.value) return
  const feedback = specFeedback.value.trim()
  specFeedback.value = ''
  entries.value.push({
    id: nextId(),
    kind: 'user',
    content: feedback || '确认规格，开始拆解工单。',
  })
  await runSse(`/api/projects/${projectId.value}/spec/confirm`, { feedback })
}

// 确认工单清单（工单 0017）：确认后进入执行期，工程师随即开始实现，
// 其后发消息不再重新澄清/拆单；调整意见可选，随确认一并落对话历史可回看；
// 首建流水线内确认不占用新名额（ADR 0003）
async function confirmTickets() {
  if (generating.value) return
  const feedback = ticketsFeedback.value.trim()
  ticketsFeedback.value = ''
  entries.value.push({
    id: nextId(),
    kind: 'user',
    content: feedback || '确认工单清单，开始执行。',
  })
  await runSse(`/api/projects/${projectId.value}/tickets/confirm`, { feedback })
}

async function runSse(path: string, body: unknown): Promise<ApiError | null> {
  generating.value = true
  errorDetail.value = ''
  interrupted.value = { status: 'none' }
  const textHolder: { entry: ChatEntry | null } = { entry: null }
  const prdHolder: { entry: ChatEntry | null } = { entry: null }
  const consensusHolder: { entry: ChatEntry | null } = { entry: null }
  const specHolder: { entry: ChatEntry | null } = { entry: null }
  const ticketsHolder: { entry: ChatEntry | null } = { entry: null }
  const thinkingHolder: { entry: ChatEntry | null } = { entry: null }

  const ensureTextEntry = (): ChatEntry => {
    if (!textHolder.entry) {
      textHolder.entry = { id: nextId(), kind: 'text', content: '', raw: '', streaming: true }
      entries.value.push(textHolder.entry)
    }
    return textHolder.entry
  }

  // 思考过程条目（诊断修复）：小号可折叠，流式时展开，随打字机逐字显现
  const ensureThinkingEntry = (): ChatEntry => {
    if (!thinkingHolder.entry) {
      thinkingHolder.entry = {
        id: nextId(),
        kind: 'thinking',
        content: '',
        raw: '',
        streaming: true,
        collapsed: false,
      }
      entries.value.push(thinkingHolder.entry)
    }
    return thinkingHolder.entry
  }

  // PRD 增量事件累积成一张流式卡片（工单 0010）；流结束后以持久化历史为准重渲染（工单 0010）
  const ensurePrdEntry = (): ChatEntry => {
    if (!prdHolder.entry) {
      prdHolder.entry = {
        id: nextId(),
        kind: 'prd',
        content: '',
        raw: '',
        streaming: true,
        prdConfirmed: false,
      }
      entries.value.push(prdHolder.entry)
    }
    return prdHolder.entry
  }

  // 需求共识增量事件累积成一张流式卡片（工单 0015）；流结束后以持久化历史为准重渲染
  const ensureConsensusEntry = (): ChatEntry => {
    if (!consensusHolder.entry) {
      consensusHolder.entry = {
        id: nextId(),
        kind: 'consensus',
        content: '',
        raw: '',
        streaming: true,
        consensusConfirmed: false,
      }
      entries.value.push(consensusHolder.entry)
    }
    return consensusHolder.entry
  }

  // 需求规格增量事件累积成一张流式卡片（工单 0016）；流结束后以持久化历史为准重渲染
  const ensureSpecEntry = (): ChatEntry => {
    if (!specHolder.entry) {
      specHolder.entry = {
        id: nextId(),
        kind: 'spec',
        content: '',
        raw: '',
        streaming: true,
        specConfirmed: false,
      }
      entries.value.push(specHolder.entry)
    }
    return specHolder.entry
  }

  // 工单清单卡片（工单 0017）：tickets 事件一次携完整清单 JSON，直接解析渲染；
  // 流结束后以持久化历史为准重渲染（同其他卡片）
  const ensureTicketsEntry = (content: string): ChatEntry => {
    if (!ticketsHolder.entry) {
      ticketsHolder.entry = {
        id: nextId(),
        kind: 'tickets',
        content,
        tickets: parseTickets(content),
        streaming: true,
        ticketsConfirmed: false,
      }
      entries.value.push(ticketsHolder.entry)
    }
    return ticketsHolder.entry
  }

  try {
    await streamPost(path, body, (event: SseEvent) => {
      if (event.type === 'thinking') {
        pushText(ensureThinkingEntry(), String(event.content ?? ''))
      } else if (event.type === 'text') {
        pushText(ensureTextEntry(), String(event.content ?? ''))
      } else if (event.type === 'prd') {
        pushText(ensurePrdEntry(), String(event.content ?? ''))
      } else if (event.type === 'consensus') {
        pushText(ensureConsensusEntry(), String(event.content ?? ''))
      } else if (event.type === 'spec') {
        pushText(ensureSpecEntry(), String(event.content ?? ''))
      } else if (event.type === 'tickets') {
        ensureTicketsEntry(String(event.content ?? ''))
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
    if (e instanceof ApiError && e.status === 429) {
      // 限流：友好提示何时可再试，而非通用报错（工单 0011）
      ElMessage.warning({ message: errorDetail.value, duration: 6000 })
      return e
    }
    return null
  } finally {
    generating.value = false
    if (textHolder.entry) textHolder.entry.streaming = false
    if (prdHolder.entry) prdHolder.entry.streaming = false
    if (consensusHolder.entry) consensusHolder.entry.streaming = false
    if (specHolder.entry) specHolder.entry.streaming = false
    if (ticketsHolder.entry) ticketsHolder.entry.streaming = false
    if (thinkingHolder.entry) thinkingHolder.entry.streaming = false
    // 等打字机追平再换历史，避免尾部字符闪现；随后以持久化结果为准对齐（含思考行，回看折叠展示）
    await waitForTypewriter()
    // 文件清单刷新后预览自动指向最新版本，无需手动刷新页面（工单 0004/0005）
    // 即便生成以 error 收尾也刷新预览：已写入的部分改动同样要可见，重试后才能对比
    await Promise.all([loadHistory(), loadFiles(), loadSnapshots()])
    previewRev.value += 1
    scrollToBottom()
  }
  return null
}
</script>

<template>
  <div class="project-page">
    <header class="topbar">
      <el-button text @click="router.push('/workspace')">← 返回</el-button>
      <span class="project-title">{{ projectName }}</span>
      <el-tag size="small" :type="projectMode === 'team' ? 'warning' : 'info'">
        {{ projectMode === 'team' ? '团队模式' : '工程师模式' }}
      </el-tag>
      <div class="publish-area">
        <template v-if="publishedSlug">
          <el-tag size="small" type="success">已发布</el-tag>
          <a
            :href="publicUrl"
            target="_blank"
            rel="noopener"
            class="public-link"
            data-testid="public-link"
          >
            {{ publicUrl }}
          </a>
          <el-button size="small" data-testid="copy-link" @click="copyLink">复制链接</el-button>
          <el-button
            size="small"
            text
            type="danger"
            data-testid="unpublish-button"
            @click="onUnpublish"
          >
            取消发布
          </el-button>
        </template>
        <el-button
          v-else
          size="small"
          type="primary"
          :loading="publishing"
          :disabled="!hasIndex"
          data-testid="publish-button"
          @click="onPublish"
        >
          发布应用
        </el-button>
      </div>
      <el-button size="small" text data-testid="history-button" @click="historyVisible = true">
        版本历史
      </el-button>
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
            <div v-else-if="entry.kind === 'prd'" class="msg agent-msg">
              <div class="prd-card">
                <div class="prd-head">
                  <span class="prd-role">产品经理 · PRD</span>
                  <el-tag v-if="entry.prdConfirmed" size="small" type="success">已确认</el-tag>
                  <el-tag v-else size="small" type="warning">待确认</el-tag>
                </div>
                <div
                  class="bubble markdown prd-body"
                  v-html="renderMarkdown(entry.content || (entry.streaming ? '正在起草 PRD…' : ''))"
                />
                <div v-if="!entry.prdConfirmed && !entry.streaming" class="prd-actions" data-testid="prd-card-actions">
                  <el-input
                    v-model="prdFeedback"
                    type="textarea"
                    :rows="2"
                    :disabled="generating"
                    placeholder="追加意见（可选），例如：界面用深色主题"
                    data-testid="prd-feedback-input"
                  />
                  <el-button
                    type="primary"
                    :loading="generating"
                    data-testid="prd-confirm-button"
                    @click="confirmPrd"
                  >
                    确认并开始实现
                  </el-button>
                </div>
              </div>
            </div>
            <div v-else-if="entry.kind === 'consensus'" class="msg agent-msg">
              <div class="prd-card consensus-card">
                <div class="prd-head">
                  <span class="prd-role">需求澄清 · 共识</span>
                  <el-tag v-if="entry.consensusConfirmed" size="small" type="success">已确认</el-tag>
                  <el-tag v-else size="small" type="warning">待确认</el-tag>
                </div>
                <div
                  class="bubble markdown prd-body"
                  v-html="renderMarkdown(entry.content || (entry.streaming ? '正在整理需求共识…' : ''))"
                />
                <div v-if="!entry.consensusConfirmed && !entry.streaming" class="prd-actions" data-testid="consensus-card-actions">
                  <el-input
                    v-model="consensusFeedback"
                    type="textarea"
                    :rows="2"
                    :disabled="generating"
                    placeholder="修改意见（可选），例如：功能 2 不要了；也可以直接继续对话补充需求"
                    data-testid="consensus-feedback-input"
                  />
                  <el-button
                    type="primary"
                    :loading="generating"
                    data-testid="consensus-confirm-button"
                    @click="confirmConsensus"
                  >
                    {{ projectMode === 'team' ? '确认共识，起草需求规格' : '确认共识并开始生成' }}
                  </el-button>
                </div>
              </div>
            </div>
            <div v-else-if="entry.kind === 'spec'" class="msg agent-msg">
              <!-- 需求规格卡片（工单 0016）：团队模式澄清收敛后的确认门，确认后进入下一阶段 -->
              <div class="prd-card spec-card">
                <div class="prd-head">
                  <span class="prd-role">团队模式 · 需求规格</span>
                  <el-tag v-if="entry.specConfirmed" size="small" type="success">已确认</el-tag>
                  <el-tag v-else size="small" type="warning">待确认</el-tag>
                </div>
                <div
                  class="bubble markdown prd-body"
                  v-html="renderMarkdown(entry.content || (entry.streaming ? '正在起草需求规格…' : ''))"
                />
                <div v-if="!entry.specConfirmed && !entry.streaming" class="prd-actions" data-testid="spec-card-actions">
                  <el-input
                    v-model="specFeedback"
                    type="textarea"
                    :rows="2"
                    :disabled="generating"
                    placeholder="修改意见（可选），例如：功能 3 换成统计页；也可以直接继续对话重新起草"
                    data-testid="spec-feedback-input"
                  />
                  <el-button
                    type="primary"
                    :loading="generating"
                    data-testid="spec-confirm-button"
                    @click="confirmSpec"
                  >
                    确认规格并开始拆解工单
                  </el-button>
                </div>
              </div>
            </div>
            <div v-else-if="entry.kind === 'tickets'" class="msg agent-msg">
              <!-- 工单清单卡片（工单 0017）：团队模式规格确认后的确认门，确认后进入执行期 -->
              <div class="prd-card tickets-card">
                <div class="prd-head">
                  <span class="prd-role">团队模式 · 工单清单</span>
                  <el-tag v-if="entry.ticketsConfirmed" size="small" type="success">已确认</el-tag>
                  <el-tag v-else size="small" type="warning">待确认</el-tag>
                </div>
                <div class="ticket-list">
                  <div v-for="t in entry.tickets ?? []" :key="t.seq" class="ticket-item">
                    <div class="ticket-head">
                      <span class="ticket-seq">#{{ t.seq }}</span>
                      <span class="ticket-title">{{ t.title }}</span>
                      <el-tag size="small" effect="plain">待执行</el-tag>
                    </div>
                    <div class="ticket-deliverable">{{ t.deliverable }}</div>
                    <div v-if="t.blocked_by.length" class="ticket-blocked">
                      被 {{ t.blocked_by.map((b) => `#${b}`).join('、') }} 阻塞，前置完成后可开始
                    </div>
                  </div>
                  <div v-if="!entry.tickets?.length" class="ticket-empty">
                    {{ entry.streaming ? '正在拆解工单…' : '工单清单为空' }}
                  </div>
                </div>
                <div v-if="!entry.ticketsConfirmed && !entry.streaming" class="prd-actions" data-testid="tickets-card-actions">
                  <el-input
                    v-model="ticketsFeedback"
                    type="textarea"
                    :rows="2"
                    :disabled="generating"
                    placeholder="调整意见（可选），例如：工单 2 拆得太细；也可以直接继续对话重新拆解"
                    data-testid="tickets-feedback-input"
                  />
                  <el-button
                    type="primary"
                    :loading="generating"
                    data-testid="tickets-confirm-button"
                    @click="confirmTickets"
                  >
                    确认清单并开始执行
                  </el-button>
                </div>
              </div>
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
            <div v-else-if="entry.kind === 'thinking'" class="msg agent-msg">
              <!-- 思考过程（诊断修复）：小一号文字，可收起/展开，随打字机逐字显现 -->
              <div class="thinking-card">
                <button
                  type="button"
                  class="thinking-toggle"
                  data-testid="thinking-toggle"
                  @click="entry.collapsed = !entry.collapsed"
                >
                  <span class="thinking-caret">{{ entry.collapsed ? '▸' : '▾' }}</span>
                  <span>思考过程</span>
                  <span v-if="entry.streaming" class="thinking-running">中…</span>
                </button>
                <div
                  v-show="!entry.collapsed"
                  class="thinking-body markdown"
                  v-html="renderMarkdown(entry.content || '…')"
                />
              </div>
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
          <div
            v-else-if="interrupted.status === 'interrupted'"
            class="error-banner"
            data-testid="chat-interrupted"
          >
            <span>⚠ 上一轮生成被中断（页面刷新或连接断开），已产出的思考过程已保留</span>
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

      <section class="right-panel">
        <div class="panel-header">
          <el-radio-group v-model="activeTab" size="small" data-testid="right-tabs">
            <el-radio-button value="preview" data-testid="tab-preview">预览</el-radio-button>
            <el-radio-button value="code" data-testid="tab-code">代码</el-radio-button>
          </el-radio-group>
        </div>
        <iframe
          v-show="activeTab === 'preview'"
          v-if="previewSrc"
          :src="previewSrc"
          class="preview-frame"
          title="应用预览"
          sandbox="allow-scripts allow-same-origin"
          data-testid="app-preview"
        />
        <el-empty
          v-if="activeTab === 'preview' && !previewSrc"
          description="预览区：生成完成的应用将在这里实时运行"
        />
        <CodeView
          v-if="activeTab === 'code'"
          :project-id="projectId"
          :files="fileList"
          :version="previewRev"
        />
      </section>
    </div>

    <el-drawer v-model="historyVisible" title="版本历史" size="360px">
      <el-empty v-if="!snapshots.length" description="每次成功生成后将自动留档一版" />
      <ul v-else class="snapshot-list" data-testid="snapshot-list">
        <li v-for="s in snapshots" :key="s.id" class="snapshot-item">
          <div class="snapshot-info">
            <div class="snapshot-title">
              版本 {{ s.rev }}
              <el-tag v-if="s.rev === snapshots[0]?.rev" size="small" type="success">最新</el-tag>
            </div>
            <div class="snapshot-meta">{{ formatTime(s.created_at) }} · {{ s.file_count }} 个文件</div>
          </div>
          <el-button
            size="small"
            :loading="rollingBackId === s.id"
            :disabled="generating || rollingBackId !== null"
            data-testid="rollback-button"
            @click="onRollback(s)"
          >
            回滚
          </el-button>
        </li>
      </ul>
    </el-drawer>
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

.publish-area {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.public-link {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #409eff;
  font-size: 12px;
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

.prd-card {
  max-width: 92%;
  border: 1px solid #ebeef5;
  border-radius: 10px;
  background: #fff;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.prd-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.prd-role {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
}

.prd-body {
  background: #f4f4f5;
  color: #303133;
  max-width: none;
}

.prd-body :deep(h1) {
  font-size: 16px;
  margin: 0 0 8px;
}

.prd-body :deep(h2) {
  font-size: 14px;
  margin: 10px 0 6px;
}

.prd-body :deep(ul),
.prd-body :deep(ol) {
  margin: 4px 0;
  padding-left: 20px;
}

.prd-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.prd-actions .el-button {
  align-self: flex-end;
}

/* 工单清单卡片（工单 0017）：逐张工单卡片展示标题/交付内容/阻塞依赖 */
.ticket-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ticket-item {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 8px 12px;
  background: #fafafa;
}

.ticket-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ticket-seq {
  font-weight: 600;
  color: #409eff;
  font-size: 13px;
}

.ticket-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  flex: 1;
}

.ticket-deliverable {
  margin-top: 4px;
  font-size: 13px;
  color: #606266;
  line-height: 1.6;
}

.ticket-blocked {
  margin-top: 4px;
  font-size: 12px;
  color: #e6a23c;
}

.ticket-empty {
  color: #909399;
  font-size: 13px;
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

/* 思考过程（诊断修复）：比正文小一号、弱化配色，左侧细线区分层次 */
.thinking-card {
  max-width: 92%;
  border-left: 2px solid #dcdfe6;
  padding: 2px 0 2px 10px;
}

.thinking-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  color: #909399;
  font-size: 12px;
}

.thinking-toggle:hover {
  color: #606266;
}

.thinking-caret {
  display: inline-block;
  width: 10px;
}

.thinking-running {
  opacity: 0.7;
}

.thinking-body {
  font-size: 12px;
  line-height: 1.6;
  color: #909399;
  margin-top: 4px;
  max-width: none;
  padding: 0;
  background: transparent;
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

.right-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #fafafa;
  min-width: 0;
}

.panel-header {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  border-bottom: 1px solid #e4e7ed;
  background: #fff;
}

.preview-frame {
  flex: 1;
  width: 100%;
  border: 0;
  background: #fff;
}

.snapshot-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.snapshot-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
}

.snapshot-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
}

.snapshot-meta {
  margin-top: 2px;
  font-size: 12px;
  color: #909399;
}
</style>
