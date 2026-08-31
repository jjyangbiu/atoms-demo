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
// 生成模式（工单 0010）：工程师直接实现 / 团队模式先产 PRD 确认后再实现
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
  <el-container class="workspace">
    <el-header class="header">
      <span class="brand">Atoms Demo</span>
      <div class="header-right">
        <el-button text data-testid="goto-world" @click="router.push({ name: 'world' })">
          App 世界
        </el-button>
        <span class="username">{{ auth.user?.username }}</span>
        <el-button text @click="onLogout">退出登录</el-button>
      </div>
    </el-header>
    <el-main>
      <div class="toolbar">
        <h2>我的项目</h2>
        <el-button type="primary" data-testid="new-project" @click="createDialogVisible = true">
          新建项目
        </el-button>
      </div>

      <el-empty v-if="store.projects.length === 0" description="还没有项目，点击右上角新建" />

      <div v-else class="project-grid">
        <el-card
          v-for="project in store.projects"
          :key="project.id"
          class="project-card"
          shadow="hover"
          :data-testid="`project-card-${project.id}`"
          @click="router.push({ name: 'project', params: { id: project.id } })"
        >
          <div class="card-head">
            <span class="project-name">{{ project.name }}</span>
            <el-tag size="small" :type="project.mode === 'team' ? 'warning' : 'info'">
              {{ project.mode === 'team' ? '团队模式' : '工程师模式' }}
            </el-tag>
          </div>
          <div class="card-meta">
            更新于 {{ formatTime(project.updated_at) }}
            <el-tag v-if="project.published_slug" size="small" type="success">已发布</el-tag>
          </div>
          <el-button
            text
            type="danger"
            size="small"
            class="delete-btn"
            @click.stop="onDelete(project.id, project.name)"
          >
            删除
          </el-button>
        </el-card>
      </div>
    </el-main>

    <el-dialog v-model="createDialogVisible" title="新建项目" width="420px">
      <el-form @submit.prevent="onCreate">
        <el-form-item label="项目名称">
          <el-input
            v-model="newName"
            placeholder="例如：番茄钟、记账工具、数据仪表盘"
            maxlength="64"
            data-testid="project-name-input"
            @keyup.enter="onCreate"
          />
        </el-form-item>
        <el-form-item label="生成模式">
          <el-radio-group v-model="newMode" data-testid="project-mode-select">
            <el-radio value="engineer">工程师模式</el-radio>
            <el-radio value="team">团队模式</el-radio>
          </el-radio-group>
        </el-form-item>
        <p class="mode-hint">
          {{
            newMode === 'team'
              ? '团队模式：产品经理先产出 PRD，确认（可附意见）后工程师才开始实现'
              : '工程师模式：智能体直接根据你的描述生成应用'
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
  </el-container>
</template>

<style scoped>
.workspace {
  height: 100%;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.brand {
  font-size: 18px;
  font-weight: 600;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.username {
  color: #606266;
  font-size: 14px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.toolbar h2 {
  margin: 0;
  font-size: 18px;
}

.project-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
}

.project-card {
  cursor: pointer;
  position: relative;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.project-name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-meta {
  margin-top: 10px;
  color: #909399;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.delete-btn {
  position: absolute;
  right: 12px;
  bottom: 8px;
}

.mode-hint {
  margin: 0;
  color: #909399;
  font-size: 12px;
}
</style>
