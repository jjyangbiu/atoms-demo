<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/projects'

const auth = useAuthStore()
const store = useProjectStore()
const router = useRouter()

const createDialogVisible = ref(false)
const newName = ref('')
// 生成模式（工单 0010/0016/0017）：工程师直接实现（先澄清）/ 团队模式澄清→规格确认→拆单确认→执行
const newMode = ref<'engineer' | 'team'>('engineer')
const creating = ref(false)

onMounted(async () => {
  await auth.fetchMe()
  await store.fetchProjects()
})

async function onLogout() {
  await auth.logout()
  router.push('/login')
}

async function onCreate() {
  const name = newName.value.trim()
  if (!name) return
  creating.value = true
  try {
    const project = await store.createProject(name, newMode.value)
    createDialogVisible.value = false
    newName.value = ''
    newMode.value = 'engineer'
    router.push({ name: 'project', params: { id: project.id } })
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? e.detail : '创建失败')
  } finally {
    creating.value = false
  }
}

async function onDelete(id: number, name: string) {
  try {
    await ElMessageBox.confirm(`确定删除项目「${name}」？此操作不可恢复。`, '删除项目', {
      type: 'warning',
    })
  } catch {
    return
  }
  await store.deleteProject(id)
  ElMessage.success('已删除')
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <div class="workspace">
    <header class="header">
      <span class="brand brand-mark">Atoms Demo</span>
      <div class="header-right">
        <el-button text data-testid="goto-world" @click="router.push({ name: 'world' })">
          App 世界
        </el-button>
        <span class="user-chip">
          <span class="user-avatar" aria-hidden="true">{{ auth.user?.username?.charAt(0) }}</span>
          <span class="username">{{ auth.user?.username }}</span>
        </span>
        <el-button text @click="onLogout">退出登录</el-button>
      </div>
    </header>

    <main class="main">
      <section class="hero">
        <div>
          <h1 class="hero-title">我的项目</h1>
          <p class="hero-sub">从一句话需求开始，让智能体为你构建可发布的单页应用</p>
        </div>
        <el-button
          type="primary"
          size="large"
          class="new-btn"
          data-testid="new-project"
          @click="createDialogVisible = true"
        >
          + 新建项目
        </el-button>
      </section>

      <div v-if="store.projects.length === 0" class="empty-card">
        <el-empty description="还没有项目，点击右上角新建">
          <el-button type="primary" @click="createDialogVisible = true">新建第一个项目</el-button>
        </el-empty>
      </div>

      <div v-else class="project-grid">
        <div
          v-for="project in store.projects"
          :key="project.id"
          class="project-card"
          role="button"
          tabindex="0"
          :data-testid="`project-card-${project.id}`"
          :aria-label="`打开项目 ${project.name}`"
          @click="router.push({ name: 'project', params: { id: project.id } })"
          @keydown.enter="router.push({ name: 'project', params: { id: project.id } })"
        >
          <div class="card-icon" aria-hidden="true">{{ project.name.charAt(0) }}</div>
          <div class="card-head">
            <span class="project-name">{{ project.name }}</span>
            <el-tag size="small" :type="project.mode === 'team' ? 'warning' : 'primary'" effect="light">
              {{ project.mode === 'team' ? '团队模式' : '工程师模式' }}
            </el-tag>
          </div>
          <div class="card-meta">
            <span class="meta-time">更新于 {{ formatTime(project.updated_at) }}</span>
            <el-tag v-if="project.published_slug" size="small" type="success" effect="light">
              已发布
            </el-tag>
          </div>
          <el-button
            text
            type="danger"
            size="small"
            class="delete-btn"
            :aria-label="`删除项目 ${project.name}`"
            @click.stop="onDelete(project.id, project.name)"
          >
            删除
          </el-button>
        </div>
      </div>
    </main>

    <el-dialog v-model="createDialogVisible" title="新建项目" width="440px">
      <el-form @submit.prevent="onCreate">
        <el-form-item label="项目名称">
          <el-input
            v-model="newName"
            size="large"
            placeholder="例如：番茄钟、记账工具、数据仪表盘"
            maxlength="64"
            data-testid="project-name-input"
            @keyup.enter="onCreate"
          />
        </el-form-item>
        <el-form-item label="生成模式">
          <el-radio-group v-model="newMode" class="mode-group" data-testid="project-mode-select">
            <el-radio value="engineer">工程师模式</el-radio>
            <el-radio value="team">团队模式</el-radio>
          </el-radio-group>
        </el-form-item>
        <p class="mode-hint">
          {{
            newMode === 'team'
              ? '团队模式：彻底澄清需求后产出需求规格，确认后拆解为工单清单，确认后按检查点串行执行'
              : '工程师模式：澄清需求并确认后，智能体根据你的描述生成应用'
          }}
        </p>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="creating"
          data-testid="project-create-submit"
          @click="onCreate"
        >
          创建并开始构建
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.workspace {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--at-bg);
}

/* ---- 顶栏 ---- */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 60px;
  padding: 0 var(--at-space-5);
  background: rgba(255, 255, 255, 0.8);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--at-border);
  position: sticky;
  top: 0;
  z-index: 10;
}

.brand {
  font-size: 17px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: var(--at-space-2);
}

.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px 4px 4px;
  border-radius: var(--at-radius-full);
  background: var(--at-primary-soft);
  border: 1px solid var(--at-primary-border);
}

.user-avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--at-gradient-brand);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
}

.username {
  color: var(--at-text);
  font-size: 13px;
  font-weight: 500;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 主区 ---- */
.main {
  flex: 1;
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: var(--at-space-6) var(--at-space-5) var(--at-space-7);
}

.hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--at-space-4);
  margin-bottom: var(--at-space-6);
}

.hero-title {
  margin: 0;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--at-text);
}

.hero-sub {
  margin: 6px 0 0;
  color: var(--at-text-muted);
  font-size: 13px;
}

.new-btn {
  font-weight: 600;
  background: var(--at-gradient-brand);
  border: none;
  padding: 20px;
}

/* ---- 空态 ---- */
.empty-card {
  background: var(--at-card);
  border: 1px dashed var(--at-border-strong);
  border-radius: var(--at-radius-lg);
  padding: var(--at-space-7) 0;
}

/* ---- 项目卡片网格 ---- */
.project-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--at-space-4);
}

.project-card {
  position: relative;
  cursor: pointer;
  background: var(--at-card);
  border: 1px solid var(--at-border);
  border-radius: var(--at-radius-md);
  padding: 20px;
  transition:
    transform var(--at-duration) var(--at-ease),
    box-shadow var(--at-duration) var(--at-ease),
    border-color var(--at-duration) var(--at-ease);
}

.project-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--at-shadow-md);
  border-color: var(--at-primary-border);
}

.card-icon {
  width: 42px;
  height: 42px;
  border-radius: var(--at-radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--at-primary-soft);
  border: 1px solid var(--at-primary-border);
  color: var(--at-primary);
  font-size: 18px;
  font-weight: 700;
  margin-bottom: 14px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--at-space-2);
}

.project-name {
  font-weight: 600;
  font-size: 15px;
  color: var(--at-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-meta {
  margin-top: 10px;
  color: var(--at-text-muted);
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: var(--at-space-2);
}

.delete-btn {
  position: absolute;
  right: 12px;
  bottom: 10px;
  opacity: 0;
  transition: opacity var(--at-duration) var(--at-ease);
}

/* 悬停/键盘聚焦时才浮现删除，避免视觉噪音；焦点可见性保障键盘可达 */
.project-card:hover .delete-btn,
.project-card:focus-within .delete-btn {
  opacity: 1;
}

/* ---- 新建对话框 ---- */
.mode-group {
  display: flex;
  gap: var(--at-space-2);
}

.mode-hint {
  margin: 0;
  padding: 10px 12px;
  color: var(--at-text-secondary);
  font-size: 12px;
  line-height: 1.6;
  background: var(--at-primary-soft);
  border: 1px solid var(--at-primary-border);
  border-radius: var(--at-radius-sm);
}

/* ---- 窄屏适配 ---- */
@media (max-width: 640px) {
  .header {
    padding: 0 var(--at-space-4);
  }

  .main {
    padding: var(--at-space-5) var(--at-space-4) var(--at-space-6);
  }

  .hero {
    flex-direction: column;
    align-items: stretch;
  }

  .new-btn {
    width: 100%;
  }

  .username {
    display: none;
  }
}
</style>
