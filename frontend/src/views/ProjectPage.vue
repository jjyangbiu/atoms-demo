<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, ApiError } from '@/api/client'
import { streamPost, type SseEvent } from '@/api/sse'
import {
  useProjectStore,
  type FileOut,
  type MessageOut,
  type SnapshotOut,
  type TicketOut,
} from '@/stores/projects'

// 代码视图异步加载：Monaco 体积较大，仅在切到代码 Tab 时才拉取（工单 0007）
const CodeView = defineAsyncComponent(() => import('@/components/CodeView.vue'))

interface ToolInfo {
  name: string
  args: Record<string, unknown>
  status: 'start' | 'done' | 'error'
  result?: string
}

// 工单清单卡片字段（工单 0017）：标题 / 交付内容 / 被谁阻塞 / 状态；
// seq 为清单内相对序号（与 blocked_by 引用同口径）；
// status/snapshot_rev 为检查点串行执行的实时状态（工单 0018，可缺省）
type TicketStatus = TicketOut['status']

interface TicketInfo {
  seq: number
  title: string
  deliverable: string
  blocked_by: number[]
  status?: TicketStatus
  snapshot_rev?: number | null
}

interface ChatEntry {
  id: string
  kind:
    | 'user'
    | 'text'
    | 'tool'
    | 'prd'
    | 'consensus'
    | 'spec'
    | 'tickets'
    | 'ticket_progress'
    | 'thinking'
    | 'clarify'
  // content 为打字机当前已显现的文本；raw 为已收到的完整增量（打字机源）
  content: string
  raw?: string
  tool?: ToolInfo
  streaming?: boolean
  // 思考块一律默认折叠（诊断修复）：流式期间与历史回看同态，想看可点开
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
  // 工单执行进度行（工单 0018）：开始/完成/失败的状态色
  ticketStatus?: TicketStatus
  // 选项式澄清问题卡片（诊断修复）：解析后的问题清单；已回答/被取代的卡片不再可点选
  clarifyQuestions?: ClarifyQuestion[]
  clarifyAnswered?: boolean
  // 澄清问答一体记录卡（工单 0020）：答案消息折叠解析结果，与 clarifyQuestions 下标对齐
  clarifyAnswers?: (string | null)[]
  // 确认记录卡默认折叠（工单 0020）：摘要 + 可展开全文，操作移入弹窗
  recordCollapsed?: boolean
  // 未确认但其后已有任何消息 = 被取代（工单 0020 取代语义）：标签展示“已被取代”而非“待确认”
  superseded?: boolean
}

interface ClarifyQuestion {
  question: string
  options: string[]
  recommend?: number | null
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

// 澄清答案折叠解析（工单 0020）：答案消息内容为「第 N 题：X」行式，
// 还原为与问题下标对齐的数组；解析不出的行为 null，记录卡仅展示问题
function parseClarifyAnswers(content: string, count: number): (string | null)[] {
  const answers: (string | null)[] = Array(count).fill(null)
  for (const line of content.split('\n')) {
    const m = /^第 (\d+) 题：(.*)$/.exec(line.trim())
    if (!m) continue
    const idx = Number(m[1]) - 1
    if (idx >= 0 && idx < count) answers[idx] = m[2]
  }
  return answers
}

// 选项式澄清清单解析（诊断修复）：后端校验已把住形状，这里只做宽容的字段摘取；
// 解析失败降级为空清单，卡片提示异常而不阻塞对话
function parseClarify(content: string): ClarifyQuestion[] {
  try {
    const data = JSON.parse(content)
    if (!Array.isArray(data)) return []
    return data
      .filter((q: unknown) => q !== null && typeof q === 'object')
      .map((item: object) => {
        const q = item as Record<string, unknown>
        return {
          question: String(q.question ?? ''),
          options: Array.isArray(q.options) ? q.options.map(String) : [],
          recommend: typeof q.recommend === 'number' ? q.recommend : null,
        }
      })
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
// 弹窗式待办动作面板（工单 0020）：每题作答状态，键为 pending 条目 id，
// 值为“问题下标 → 答案”；选项与自定义互斥，收起期间保留，历史重建后失效
interface PanelAnswer {
  kind: 'option' | 'custom'
  text: string
}
const panelAnswers = ref<Record<string, Record<number, PanelAnswer>>>({})
// 弹窗张开状态：pending 出现（含刷新重建）自动张开；取消为非破坏性收起
const panelOpen = ref(false)
// 问答模板翻页：一题一页、1/N 左右翻
const panelPage = ref(0)
// 工单执行状态（工单 0018）：来自 /tickets 接口，断线重连后据此展示进度与继续/重试入口；
// 卡片内按清单下标对齐（卡片内序号是相对编号，重拆后会续编）
const ticketStates = ref<TicketOut[]>([])
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

// 工单执行进度文案（工单 0018）：SSE 实时事件与历史 kind=ticket 行共用同一口径
function ticketProgressText(p: {
  seq: number
  title?: string
  status: string
  snapshot_rev?: number | null
}): string {
  const name = p.title ? `「${p.title}」` : ''
  if (p.status === 'running') return `▶ 正在执行工单 ${p.seq}${name}…`
  if (p.status === 'done') {
    const checkpoint = p.snapshot_rev ? `，检查点版本 ${p.snapshot_rev}` : ''
    return `✓ 工单 ${p.seq}${name} 完成${checkpoint}`
  }
  return `✗ 工单 ${p.seq}${name} 执行失败`
}

// 执行期判定（工单 0018）：执行已启动（有非 open 状态）且尚未全部完成；
// 断线重连后据此展示“继续执行/重试”入口，并抑制通用的“重新生成”中断横幅
const execActive = computed(() => {
  if (projectMode.value !== 'team' || ticketStates.value.length === 0) return false
  const started = ticketStates.value.some((t) => t.status !== 'open')
  const finished = ticketStates.value.every((t) => t.status === 'done')
  return started && !finished
})

const failedTicket = computed(() => ticketStates.value.find((t) => t.status === 'failed') ?? null)
const doneTicketCount = computed(() => ticketStates.value.filter((t) => t.status === 'done').length)

function isLastTicketsEntry(entry: ChatEntry): boolean {
  for (let i = entries.value.length - 1; i >= 0; i--) {
    if (entries.value[i].kind === 'tickets') return entries.value[i] === entry
  }
  return false
}

// 卡片内单张工单的展示状态（工单 0018）：实时事件更新过的优先，其次按接口状态对齐（仅最新卡片）
function ticketDisplay(entry: ChatEntry, t: TicketInfo): { status: TicketStatus; snapshot_rev: number | null } {
  if (t.status) return { status: t.status, snapshot_rev: t.snapshot_rev ?? null }
  if (entry.ticketsConfirmed && isLastTicketsEntry(entry)) {
    const idx = entry.tickets?.indexOf(t) ?? -1
    const state = idx >= 0 ? ticketStates.value[idx] : undefined
    if (state) return { status: state.status, snapshot_rev: state.snapshot_rev }
  }
  return { status: 'open', snapshot_rev: null }
}

function ticketStatusLabel(d: { status: TicketStatus; snapshot_rev: number | null }): string {
  if (d.status === 'running') return '执行中'
  if (d.status === 'done') return d.snapshot_rev ? `完成 · 检查点 v${d.snapshot_rev}` : '完成'
  if (d.status === 'failed') return '失败'
  return '待执行'
}

function ticketTagType(status: TicketStatus): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'done') return 'success'
  if (status === 'running') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
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
        recordCollapsed: true,
        superseded: !confirmed && messages.slice(i + 1).length > 0,
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
        recordCollapsed: true,
        superseded: !confirmed && messages.slice(i + 1).length > 0,
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
        recordCollapsed: true,
        superseded: !confirmed && messages.slice(i + 1).length > 0,
      })
    } else if (m.kind === 'ticket') {
      // 工单执行进度行（工单 0018）：完成/失败留痕，刷新后回看执行过程
      try {
        const p = JSON.parse(m.content) as {
          seq: number
          title?: string
          status: string
          snapshot_rev?: number | null
        }
        result.push({
          id: `msg-${m.id}`,
          kind: 'ticket_progress',
          content: ticketProgressText(p),
          ticketStatus: p.status as TicketStatus,
        })
      } catch {
        /* 进度行解析失败则跳过 */
      }
    } else if (m.kind === 'clarify') {
      // 选项式澄清问题卡片（诊断修复）：其后存在任何消息（回答/新一轮提问）即视为已回答或被取代，不再可点选
      result.push({
        id: `msg-${m.id}`,
        kind: 'clarify',
        content: m.content,
        clarifyQuestions: parseClarify(m.content),
        clarifyAnswered: messages.slice(i + 1).length > 0,
      })
    } else if (m.kind === 'clarify_answer') {
      // 澄清答案（工单 0020）：不渲染独立气泡，折叠进对应问题记录卡；
      // 找不到对应问题卡（脏数据）时降级为用户气泡，不丢消息
      const target = [...result].reverse().find((e) => e.kind === 'clarify')
      if (target?.clarifyQuestions) {
        target.clarifyAnswers = parseClarifyAnswers(m.content, target.clarifyQuestions.length)
      } else {
        result.push({ id: `msg-${m.id}`, kind: 'user', content: m.content })
      }
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
// 恢复为可重试内容（刷新后 lastUserContent 已丢失，重试按钮才可用）。
// 工单执行中断除外（工单 0018）：正确的恢复入口是清单卡片上的“继续执行”，
// 重发用户消息会被执行期引导拦截，横幅只会误导。
function detectInterrupted(messages: MessageOut[]) {
  if (execActive.value) {
    interrupted.value = { status: 'none' }
    return
  }
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

// 工单执行状态（工单 0018）：团队模式拉取，断线重连后卡片与继续/重试入口据此重建；
// 必须先于历史重建完成，中断识别才能据执行期抑制“重新生成”横幅改用继续执行。
// loading 守卫：会话内首次执行时事件触发补拉，防多事件重复拉取。
const ticketStatesLoading = ref(false)
async function loadTickets() {
  if (projectMode.value !== 'team') {
    ticketStates.value = []
    return
  }
  if (ticketStatesLoading.value) return
  ticketStatesLoading.value = true
  try {
    ticketStates.value = await store.fetchTickets(projectId.value)
  } finally {
    ticketStatesLoading.value = false
  }
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString()
}

async function onRollback(snapshot: SnapshotOut) {
  // 团队模式回滚到检查点：其后工单重置为待执行，由「继续执行」续跑（工单 0019）
  const checkpointNote = snapshot.ticket_seq
    ? `（工单 ${snapshot.ticket_seq} 的检查点，其后的工单将重置为待执行，可再「继续执行」）`
    : ''
  try {
    await ElMessageBox.confirm(
      `回滚到版本 ${snapshot.rev}${checkpointNote} 后，当前文件将被替换为该版本状态，后续迭代以其为基线。确定回滚？`,
      '回滚版本',
      { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  rollingBackId.value = snapshot.id
  try {
    await store.rollbackSnapshot(projectId.value, snapshot.id)
    // 回滚可能重置工单状态（工单 0019）：工单态一并重拉，继续执行入口据最新状态重建
    await Promise.all([loadFiles(), loadSnapshots(), loadTickets()])
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
  await Promise.all([loadTickets(), loadFiles(), loadSnapshots()])
  await loadHistory()
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
  panelOpen.value = false
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
  panelOpen.value = false
  const feedback = specFeedback.value.trim()
  specFeedback.value = ''
  entries.value.push({
    id: nextId(),
    kind: 'user',
    content: feedback || '确认规格，开始拆解工单。',
  })
  await runSse(`/api/projects/${projectId.value}/spec/confirm`, { feedback })
}

// 确认工单清单（工单 0017）：确认后进入执行期，工程师按检查点串行逐单实现（工单 0018），
// 其后发消息不再重新澄清/拆单；调整意见可选，随确认一并落对话历史可回看；
// 首建流水线内确认与执行都不占用新名额（ADR 0003）
async function confirmTickets() {
  if (generating.value) return
  panelOpen.value = false
  const feedback = ticketsFeedback.value.trim()
  ticketsFeedback.value = ''
  entries.value.push({
    id: nextId(),
    kind: 'user',
    content: feedback || '确认工单清单，开始执行。',
  })
  await runSse(`/api/projects/${projectId.value}/tickets/confirm`, { feedback })
}

// 继续/重试工单执行（工单 0018）：从第一张未完成工单起点继续，已完成的单不重跑；
// 首建流水线内不重复占名额，断线重连与失败重试同一入口；全部完成后后端返 409，
// 前端在流结束后以 /tickets 状态为准重建卡片，无需额外处理。
async function resumeTickets() {
  if (generating.value) return
  await runSse(`/api/projects/${projectId.value}/tickets/resume`, {})
}

// 待办动作检测（工单 0020）：尾部卡片未回答/未确认 = 当前唯一 pending 动作；
// 流式期间（generating/streaming）不算 pending，弹窗等流结束历史重建后再弹
const pendingAction = computed(() => {
  if (generating.value) return null
  const last = entries.value[entries.value.length - 1]
  if (!last || last.streaming) return null
  if (last.kind === 'clarify' && !last.clarifyAnswered)
    return { entryId: last.id, kind: 'clarify' as const, entry: last }
  if (last.kind === 'consensus' && !last.consensusConfirmed && !last.superseded)
    return { entryId: last.id, kind: 'consensus' as const, entry: last }
  if (last.kind === 'spec' && !last.specConfirmed && !last.superseded)
    return { entryId: last.id, kind: 'spec' as const, entry: last }
  if (last.kind === 'tickets' && !last.ticketsConfirmed && !last.superseded)
    return { entryId: last.id, kind: 'tickets' as const, entry: last }
  return null
})

const pendingKey = computed(() => pendingAction.value?.entryId ?? null)

// pending 切换（新一轮提问/新草案/刷新重建）→ 自动张弹窗；pending 消失 → 收起（工单 0020）
watch(pendingKey, (now, prev) => {
  if (now && now !== prev) {
    panelOpen.value = true
    panelPage.value = 0
  } else if (!now) {
    panelOpen.value = false
  }
})

const pendingLabel = computed(() => {
  switch (pendingAction.value?.kind) {
    case 'clarify':
      return '需求澄清待回答'
    case 'consensus':
      return '需求共识待确认'
    case 'spec':
      return '需求规格待确认'
    case 'tickets':
      return '工单清单待确认'
    default:
      return ''
  }
})

const panelQuestions = computed(() => pendingAction.value?.entry.clarifyQuestions ?? [])

// 全部题目都有答案（选项或自定义非空）才启用“继续”（工单 0020）
const panelAllAnswered = computed(() => {
  const p = pendingAction.value
  if (!p || p.kind !== 'clarify' || !panelQuestions.value.length) return false
  const per = panelAnswers.value[p.entryId] ?? {}
  return panelQuestions.value.every((_q, i) => (per[i]?.text ?? '').trim().length > 0)
})

function panelAnswerOf(qIdx: number): PanelAnswer | undefined {
  const p = pendingAction.value
  if (!p) return undefined
  return (panelAnswers.value[p.entryId] ?? {})[qIdx]
}

function pickPanelOption(qIdx: number, opt: string) {
  const p = pendingAction.value
  if (!p || generating.value) return
  const per = panelAnswers.value[p.entryId] ?? {}
  per[qIdx] = { kind: 'option', text: opt }
  panelAnswers.value[p.entryId] = per
  // 选中选项即自动切到下一题（诊断修复）；末题停留原页，等用户主动点「继续」提交
  if (panelPage.value === qIdx && qIdx < panelQuestions.value.length - 1) {
    panelPage.value = qIdx + 1
  }
}

function setPanelCustom(qIdx: number, text: string) {
  const p = pendingAction.value
  if (!p || generating.value) return
  const per = panelAnswers.value[p.entryId] ?? {}
  per[qIdx] = { kind: 'custom', text }
  panelAnswers.value[p.entryId] = per
}

function cancelPanel() {
  // 非破坏性收起（工单 0020）：输入框恢复可用，重开入口出现
  panelOpen.value = false
}

function reopenPanel() {
  panelOpen.value = true
  panelPage.value = 0
}

// 确认记录卡摘要（工单 0020）：折叠时只展示首个非空行截断，展开看全文
function excerptOf(content: string): string {
  const line = content
    .split('\n')
    .map((s) => s.trim())
    .find((s) => s.length > 0)
  const plain = (line ?? '').replace(/^#+\s*/, '').replace(/[*_`>]/g, '')
  if (!plain) return '（空内容）'
  return plain.length > 60 ? `${plain.slice(0, 60)}…` : plain
}

function ticketsExcerpt(entry: ChatEntry): string {
  const ts = entry.tickets ?? []
  const head = ts
    .slice(0, 3)
    .map((t) => `#${t.seq} ${t.title}`)
    .join('；')
  return `共 ${ts.length} 张工单${head ? '：' + head : ''}${ts.length > 3 ? '…' : ''}`
}

// 弹窗追加意见输入复用各阶段意见状态（工单 0020）
const panelFeedback = computed({
  get() {
    switch (pendingAction.value?.kind) {
      case 'consensus':
        return consensusFeedback.value
      case 'spec':
        return specFeedback.value
      case 'tickets':
        return ticketsFeedback.value
      default:
        return ''
    }
  },
  set(v: string) {
    switch (pendingAction.value?.kind) {
      case 'consensus':
        consensusFeedback.value = v
        break
      case 'spec':
        specFeedback.value = v
        break
      case 'tickets':
        ticketsFeedback.value = v
        break
    }
  },
})

const panelConfirmLabel = computed(() => {
  switch (pendingAction.value?.kind) {
    case 'consensus':
      return projectMode.value === 'team' ? '确认共识，起草规格' : '确认共识并开始生成'
    case 'spec':
      return '确认规格并开始拆单'
    case 'tickets':
      return '确认清单并开始执行'
    default:
      return '确认'
  }
})

function confirmByKind() {
  const kind = pendingAction.value?.kind
  if (kind === 'consensus') void confirmConsensus()
  else if (kind === 'spec') void confirmSpec()
  else if (kind === 'tickets') void confirmTickets()
}

// 弹窗提交澄清回答 = 发送一条携答案标记的用户消息（工单 0020）：
// 后端分流/名额语义不变；提交后问答一体记录卡立即呈现，流结束后以持久化历史为准重建
async function submitPanelAnswers() {
  const p = pendingAction.value
  if (!p || generating.value || !panelAllAnswered.value) return
  const per = panelAnswers.value[p.entryId] ?? {}
  const parts: string[] = []
  panelQuestions.value.forEach((_q, i) => {
    const text = (per[i]?.text ?? '').trim()
    if (text) parts.push(`第 ${i + 1} 题：${text}`)
  })
  const content = parts.join('\n')
  p.entry.clarifyAnswered = true
  p.entry.clarifyAnswers = panelQuestions.value.map((_q, i) => (per[i]?.text ?? '').trim())
  panelOpen.value = false
  lastUserContent = content
  await runSse(`/api/projects/${projectId.value}/messages`, { content, clarify_answer: true })
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

  // 思考过程条目（诊断修复）：小号可折叠，一律默认折叠，随打字机逐字显现（点开才可见）
  const ensureThinkingEntry = (): ChatEntry => {
    if (!thinkingHolder.entry) {
      thinkingHolder.entry = {
        id: nextId(),
        kind: 'thinking',
        content: '',
        raw: '',
        streaming: true,
        collapsed: true,
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

  // 新一轮输出的起点（工具事件开始/工单进度行）即收束当前思考/文本条目：
  // 其后到来的 thinking/text 增量另开新条目排在事件行之后，
  // 避免多步循环/多张工单后续轮次的增量并入第一轮创建的条目（诊断修复）
  const closeStreamingSegments = (): void => {
    if (thinkingHolder.entry) thinkingHolder.entry.streaming = false
    thinkingHolder.entry = null
    if (textHolder.entry) textHolder.entry.streaming = false
    textHolder.entry = null
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
      } else if (event.type === 'clarify') {
        // 选项式澄清问题卡片（诊断修复）：事件一次携完整清单，直接解析渲染；
        // 流结束后以持久化历史为准重渲染（同其他卡片）
        const content = String(event.content ?? '')
        entries.value.push({
          id: nextId(),
          kind: 'clarify',
          content,
          clarifyQuestions: parseClarify(content),
          clarifyAnswered: false,
        })
      } else if (event.type === 'ticket_progress') {
        // 工单执行进度（工单 0018）：同步卡片内工单状态与接口态，追加一行进度供实时可见；
        // 卡片内序号是相对编号，按清单下标对齐服务端 seq（重拆后续编也不错位）；
        // 会话内首次“确认即执行”时接口态尚未拉取（挂载时清单尚不存在），
        // 补拉一次后后续事件与进度条即可按接口态对齐（断线重连场景挂载时已拉取）
        closeStreamingSegments()
        if (ticketStates.value.length === 0) void loadTickets()
        const seq = Number(event.seq)
        const st = String(event.status ?? '') as TicketStatus
        const rev = typeof event.snapshot_rev === 'number' ? event.snapshot_rev : null
        const stateIdx = ticketStates.value.findIndex((t) => t.seq === seq)
        const card = [...entries.value].reverse().find((e) => e.kind === 'tickets')
        const ticketInCard =
          card?.tickets && stateIdx >= 0 ? card.tickets[stateIdx] : undefined
        if (ticketInCard) {
          ticketInCard.status = st
          if (rev !== null) ticketInCard.snapshot_rev = rev
        }
        if (stateIdx >= 0) {
          ticketStates.value[stateIdx].status = st
          if (rev !== null) ticketStates.value[stateIdx].snapshot_rev = rev
        }
        entries.value.push({
          id: nextId(),
          kind: 'ticket_progress',
          content: ticketProgressText({
            seq,
            title: typeof event.title === 'string' ? event.title : undefined,
            status: st,
            snapshot_rev: rev,
          }),
          ticketStatus: st,
        })
      } else if (event.type === 'tool') {
        const tool = {
          name: String(event.name ?? ''),
          args: (event.args as Record<string, unknown>) ?? {},
          status: (event.status as ToolInfo['status']) ?? 'done',
          result: event.result as string | undefined,
        }
        if (tool.status === 'start') {
          closeStreamingSegments()
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
        // 错误收尾即收起弹窗（工单 0020）：pending 仍在的话重开入口会出现
        panelOpen.value = false
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
    // 即便生成以 error 收尾也刷新预览：已写入的部分改动同样要可见，重试后才能对比；
    // 工单执行状态同步重拉，失败/中断后的继续入口据最新状态重建（工单 0018）
    await Promise.all([loadHistory(), loadFiles(), loadSnapshots(), loadTickets()])
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
            <div v-else-if="entry.kind === 'clarify'" class="msg agent-msg">
              <!-- 澄清问答一体记录卡（工单 0020）：操作移入弹窗，此处只读留痕；
                   已回答展示问题+折叠答案；存量无标记旧卡仅展示问题；待回答展示问题+待选标签 -->
              <div class="prd-card clarify-card">
                <div class="prd-head">
                  <span class="prd-role">需求澄清 · 问答记录</span>
                  <el-tag v-if="entry.clarifyAnswered" size="small" type="success">已处理</el-tag>
                  <el-tag v-else size="small" type="warning">待选择</el-tag>
                </div>
                <div class="clarify-list">
                  <div v-for="(q, qi) in entry.clarifyQuestions ?? []" :key="qi" class="clarify-question">
                    <div class="clarify-q-text">{{ qi + 1 }}. {{ q.question }}</div>
                    <div v-if="entry.clarifyAnswers?.[qi]" class="clarify-a-text">
                      答：{{ entry.clarifyAnswers[qi] }}
                    </div>
                  </div>
                  <div v-if="!entry.clarifyQuestions?.length" class="clarify-empty">
                    澄清问题内容解析失败，可收起弹窗后直接在输入框回答
                  </div>
                </div>
              </div>
            </div>
            <div v-else-if="entry.kind === 'consensus'" class="msg agent-msg">
              <div class="prd-card consensus-card">
                <div class="prd-head">
                  <span class="prd-role">需求澄清 · 共识</span>
                  <el-tag v-if="entry.consensusConfirmed" size="small" type="success">已确认</el-tag>
                  <el-tag v-else-if="entry.superseded" size="small" type="info">已被取代</el-tag>
                  <el-tag v-else size="small" type="warning">待确认</el-tag>
                </div>
                <div
                  v-if="entry.streaming || !entry.recordCollapsed"
                  class="bubble markdown prd-body"
                  v-html="renderMarkdown(entry.content || (entry.streaming ? '正在整理需求共识…' : ''))"
                />
                <div v-else class="record-excerpt">{{ excerptOf(entry.content) }}</div>
                <button
                  v-if="!entry.streaming"
                  type="button"
                  class="record-toggle"
                  data-testid="consensus-record-toggle"
                  @click="entry.recordCollapsed = !entry.recordCollapsed"
                >
                  {{ entry.recordCollapsed ? '展开全文' : '收起全文' }}
                </button>
              </div>
            </div>
            <div v-else-if="entry.kind === 'spec'" class="msg agent-msg">
              <!-- 需求规格卡片（工单 0016）：团队模式澄清收敛后的确认门，确认后进入下一阶段 -->
              <div class="prd-card spec-card">
                <div class="prd-head">
                  <span class="prd-role">团队模式 · 需求规格</span>
                  <el-tag v-if="entry.specConfirmed" size="small" type="success">已确认</el-tag>
                  <el-tag v-else-if="entry.superseded" size="small" type="info">已被取代</el-tag>
                  <el-tag v-else size="small" type="warning">待确认</el-tag>
                </div>
                <div
                  v-if="entry.streaming || !entry.recordCollapsed"
                  class="bubble markdown prd-body"
                  v-html="renderMarkdown(entry.content || (entry.streaming ? '正在起草需求规格…' : ''))"
                />
                <div v-else class="record-excerpt">{{ excerptOf(entry.content) }}</div>
                <button
                  v-if="!entry.streaming"
                  type="button"
                  class="record-toggle"
                  data-testid="spec-record-toggle"
                  @click="entry.recordCollapsed = !entry.recordCollapsed"
                >
                  {{ entry.recordCollapsed ? '展开全文' : '收起全文' }}
                </button>
              </div>
            </div>
            <div v-else-if="entry.kind === 'tickets'" class="msg agent-msg">
              <!-- 工单清单卡片（工单 0017）：团队模式规格确认后的确认门，确认后进入执行期 -->
              <div class="prd-card tickets-card">
                <div class="prd-head">
                  <span class="prd-role">团队模式 · 工单清单</span>
                  <el-tag v-if="entry.ticketsConfirmed" size="small" type="success">已确认</el-tag>
                  <el-tag v-else-if="entry.superseded" size="small" type="info">已被取代</el-tag>
                  <el-tag v-else size="small" type="warning">待确认</el-tag>
                </div>
                <div v-show="entry.streaming || !entry.recordCollapsed" class="ticket-list">
                  <div v-for="t in entry.tickets ?? []" :key="t.seq" class="ticket-item">
                    <div class="ticket-head">
                      <span class="ticket-seq">#{{ t.seq }}</span>
                      <span class="ticket-title">{{ t.title }}</span>
                      <!-- 执行状态（工单 0018）：待执行/执行中/完成（附检查点版本）/失败 -->
                      <el-tag
                        size="small"
                        effect="plain"
                        :type="ticketTagType(ticketDisplay(entry, t).status)"
                      >
                        {{ ticketStatusLabel(ticketDisplay(entry, t)) }}
                      </el-tag>
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
                <!-- 执行进度与继续/重试入口（工单 0018）：仅最新已确认卡片展示，
                     断线重连后据 /tickets 状态重建；失败时从失败单重试，否则继续执行 -->
                <div
                  v-if="entry.ticketsConfirmed && !entry.streaming && isLastTicketsEntry(entry) && ticketStates.length"
                  class="ticket-exec-bar"
                  data-testid="tickets-exec-bar"
                >
                  <span class="ticket-progress">
                    执行进度 {{ doneTicketCount }}/{{ ticketStates.length }}
                  </span>
                  <el-tag v-if="!execActive" size="small" type="success">全部完成</el-tag>
                  <el-button
                    v-else
                    type="primary"
                    size="small"
                    :disabled="generating"
                    data-testid="tickets-resume-button"
                    @click="resumeTickets"
                  >
                    {{ failedTicket ? `重试工单 ${failedTicket.seq}` : '继续执行' }}
                  </el-button>
                </div>
                <div v-if="!entry.streaming && entry.recordCollapsed" class="record-excerpt">
                  {{ ticketsExcerpt(entry) }}
                </div>
                <button
                  v-if="!entry.streaming"
                  type="button"
                  class="record-toggle"
                  data-testid="tickets-record-toggle"
                  @click="entry.recordCollapsed = !entry.recordCollapsed"
                >
                  {{ entry.recordCollapsed ? '展开全文' : '收起全文' }}
                </button>
              </div>
            </div>
            <div v-else-if="entry.kind === 'ticket_progress'" class="tool-line">
              <!-- 工单执行进度行（工单 0018）：开始/完成（含检查点版本）/失败，历史回看由消息行重建 -->
              <el-tag
                :type="entry.ticketStatus === 'failed' ? 'danger' : entry.ticketStatus === 'done' ? 'success' : 'warning'"
                size="small"
                effect="plain"
              >
                {{ entry.content }}
              </el-tag>
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

        <div class="chat-bottom">
          <!-- 待办重开入口（工单 0020）：弹窗取消收起后出现，点击恢复弹窗 -->
          <div v-if="pendingAction && !panelOpen" class="pending-reopen" data-testid="pending-reopen">
            <span>⏳ {{ pendingLabel }}</span>
            <el-button size="small" type="primary" data-testid="pending-reopen-button" @click="reopenPanel">
              处理
            </el-button>
          </div>
          <!-- 弹窗式待办动作面板（工单 0020）：悬浮于输入框上方、无遮罩；
               问答模板（澄清）与审阅模板（共识/规格/工单）共用容器 -->
          <div v-if="pendingAction && panelOpen" class="pending-panel" data-testid="pending-panel">
            <div class="pending-panel-head">
              <span class="pending-panel-title">
                {{ pendingAction.kind === 'clarify' ? '请回答以下问题' : pendingLabel }}
              </span>
              <span v-if="pendingAction.kind === 'clarify'" class="pending-pager">
                <button type="button" :disabled="panelPage <= 0" @click="panelPage -= 1">‹</button>
                <span>{{ panelPage + 1 }}/{{ panelQuestions.length }}</span>
                <button
                  type="button"
                  :disabled="panelPage >= panelQuestions.length - 1"
                  @click="panelPage += 1"
                >
                  ›
                </button>
              </span>
            </div>
            <div v-if="pendingAction.kind === 'clarify'" class="pending-panel-body">
              <template v-if="panelQuestions.length">
                <div class="panel-q-text">{{ panelPage + 1 }}. {{ panelQuestions[panelPage]?.question }}</div>
                <div class="panel-options">
                  <button
                    v-for="(opt, oi) in panelQuestions[panelPage]?.options ?? []"
                    :key="oi"
                    type="button"
                    class="panel-option"
                    :class="{
                      selected: panelAnswerOf(panelPage)?.kind === 'option' && panelAnswerOf(panelPage)?.text === opt,
                    }"
                    :data-testid="`panel-option-${panelPage}-${oi}`"
                    @click="pickPanelOption(panelPage, opt)"
                  >
                    <span class="panel-option-badge">{{ String.fromCharCode(65 + oi) }}</span>
                    {{ opt }}
                    <span v-if="panelQuestions[panelPage]?.recommend === oi" class="clarify-recommend">
                      ➤ 推荐
                    </span>
                  </button>
                  <div
                    class="panel-option panel-option-custom"
                    :class="{ selected: panelAnswerOf(panelPage)?.kind === 'custom' }"
                    @click="
                      setPanelCustom(
                        panelPage,
                        panelAnswerOf(panelPage)?.kind === 'custom'
                          ? (panelAnswerOf(panelPage)?.text ?? '')
                          : '',
                      )
                    "
                  >
                    <span class="panel-option-badge">
                      {{ String.fromCharCode(65 + (panelQuestions[panelPage]?.options ?? []).length) }}
                    </span>
                    <el-input
                      :model-value="panelAnswerOf(panelPage)?.kind === 'custom' ? (panelAnswerOf(panelPage)?.text ?? '') : ''"
                      placeholder="或输入自定义答案"
                      :data-testid="`panel-custom-${panelPage}`"
                      @input="setPanelCustom(panelPage, $event)"
                    />
                  </div>
                </div>
              </template>
              <div v-else class="clarify-empty">澄清问题内容解析失败，可取消后直接在输入框回答</div>
            </div>
            <div v-else class="pending-panel-body pending-review">
              <div
                v-if="pendingAction.kind !== 'tickets'"
                class="bubble markdown prd-body review-doc"
                v-html="renderMarkdown(pendingAction.entry.content)"
              />
              <div v-else class="ticket-list review-doc">
                <div v-for="t in pendingAction.entry.tickets ?? []" :key="t.seq" class="ticket-item">
                  <div class="ticket-head">
                    <span class="ticket-seq">#{{ t.seq }}</span>
                    <span class="ticket-title">{{ t.title }}</span>
                  </div>
                  <div class="ticket-deliverable">{{ t.deliverable }}</div>
                  <div v-if="t.blocked_by.length" class="ticket-blocked">
                    被 {{ t.blocked_by.map((b) => `#${b}`).join('、') }} 阻塞
                  </div>
                </div>
              </div>
              <el-input
                v-model="panelFeedback"
                type="textarea"
                :rows="2"
                placeholder="追加意见（可选）"
                data-testid="panel-feedback"
              />
            </div>
            <div class="pending-panel-foot">
              <el-button size="small" data-testid="panel-cancel" @click="cancelPanel">取消</el-button>
              <el-button
                v-if="pendingAction.kind === 'clarify'"
                size="small"
                type="primary"
                :disabled="!panelAllAnswered"
                :loading="generating"
                data-testid="panel-continue"
                @click="submitPanelAnswers"
              >
                继续
              </el-button>
              <el-button
                v-else
                size="small"
                type="primary"
                :loading="generating"
                data-testid="panel-confirm"
                @click="confirmByKind"
              >
                {{ panelConfirmLabel }}
              </el-button>
            </div>
          </div>
          <div class="chat-input">
            <el-input
              v-model="input"
              type="textarea"
              :rows="3"
              :disabled="generating || panelOpen"
              placeholder="描述需求或提出修改，例如：把按钮改大一点"
              data-testid="chat-input"
              @keydown.enter.exact.prevent="send"
            />
            <el-button
              type="primary"
              :loading="generating"
              :disabled="!input.trim() || panelOpen"
              data-testid="chat-send"
              @click="send"
            >
              {{ generating ? '生成中' : '发送' }}
            </el-button>
          </div>
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
              <!-- 检查点标识（工单 0019）：回滚到检查点会重置其后工单并可续跑 -->
              <el-tag v-if="s.ticket_seq" size="small" type="warning">
                工单 {{ s.ticket_seq }} 检查点
              </el-tag>
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

/* 选项式澄清问题卡片（诊断修复）：逐题候选项横排可点选，推荐项虚线标注，选中项高亮 */
.clarify-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.clarify-q-text {
  font-size: 13px;
  color: #303133;
  margin-bottom: 6px;
}

.clarify-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.clarify-option {
  border: 1px solid #dcdfe6;
  border-radius: 16px;
  background: #fff;
  padding: 4px 12px;
  font-size: 13px;
  color: #606266;
  cursor: pointer;
  line-height: 1.5;
}

.clarify-option:hover:not(:disabled) {
  border-color: #409eff;
  color: #409eff;
}

.clarify-option.selected {
  border-color: #409eff;
  background: #ecf5ff;
  color: #409eff;
}

.clarify-option.recommended {
  border-style: dashed;
}

.clarify-option:disabled {
  cursor: default;
  opacity: 0.75;
}

.clarify-recommend {
  margin-left: 6px;
  font-size: 12px;
  color: #e6a23c;
}

.clarify-empty {
  font-size: 13px;
  color: #909399;
}

.clarify-hint {
  font-size: 12px;
  color: #909399;
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

/* 工单执行进度条（工单 0018）：整体进度 + 继续/重试入口 */
.ticket-exec-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 4px;
  border-top: 1px dashed #ebeef5;
}

.ticket-progress {
  font-size: 13px;
  color: #606266;
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
}

/* 弹窗式待办动作面板（工单 0020）：输入区容器作为定位错，面板悬浮其上、无遮罩 */
.chat-bottom {
  position: relative;
  border-top: 1px solid #e4e7ed;
}

.pending-reopen {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 16px;
  background: #fdf6ec;
  color: #e6a23c;
  font-size: 13px;
}

.pending-panel {
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: 100%;
  margin-bottom: 8px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  max-height: min(480px, 70vh);
  z-index: 10;
}

.pending-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid #ebeef5;
}

.pending-panel-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
}

.pending-pager {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #909399;
}

.pending-pager button {
  border: 1px solid #dcdfe6;
  background: #fff;
  border-radius: 4px;
  width: 20px;
  height: 20px;
  cursor: pointer;
  color: #606266;
}

.pending-pager button:disabled {
  opacity: 0.4;
  cursor: default;
}

.pending-panel-body {
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.panel-q-text {
  font-size: 13px;
  color: #303133;
}

.panel-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.panel-option {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: #f4f4f5;
  padding: 8px 10px;
  font-size: 13px;
  color: #303133;
  cursor: pointer;
  text-align: left;
}

.panel-option:hover {
  background: #ecf5ff;
}

.panel-option.selected {
  background: #ecf5ff;
  border-color: #409eff;
  color: #409eff;
}

.panel-option-badge {
  flex: none;
  width: 18px;
  height: 18px;
  border: 1px solid #c0c4cc;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: #606266;
  background: #fff;
}

.panel-option.selected .panel-option-badge {
  border-color: #409eff;
  color: #409eff;
}

.panel-option-custom {
  cursor: text;
}

.panel-option-custom .el-input {
  flex: 1;
}

.review-doc {
  max-width: none;
}

.pending-panel-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 8px 12px;
  border-top: 1px solid #ebeef5;
}

/* 确认记录卡折叠摘要与展开入口（工单 0020） */
.record-excerpt {
  font-size: 13px;
  color: #909399;
  padding: 2px 4px;
}

.record-toggle {
  align-self: flex-start;
  border: none;
  background: none;
  color: #409eff;
  font-size: 12px;
  cursor: pointer;
  padding: 0 4px;
}

.record-toggle:hover {
  color: #66b1ff;
}

/* 澄清问答一体记录卡的答案行（工单 0020） */
.clarify-a-text {
  font-size: 13px;
  color: #409eff;
  padding-left: 12px;
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
