<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError, getToken } from '@/api/client'
import { useWorldStore, type WorldAppOut } from '@/stores/world'

const store = useWorldStore()
const route = useRoute()
const router = useRouter()

const slug = String(route.params.slug)
const app = ref<WorldAppOut | null>(null)
const notFound = ref(false)
const cloning = ref(false)

onMounted(async () => {
  try {
    app.value = await store.fetchWorldApp(slug)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      notFound.value = true
    } else {
      ElMessage.error(e instanceof ApiError ? e.detail : '加载失败，请稍后重试')
    }
  }
})

async function onClone() {
  // 未登录点击克隆：引导到登录/注册，登录后回到本页（工单 0008 验收项）
  if (!getToken()) {
    router.push({ name: 'login', query: { redirect: route.fullPath } })
    return
  }
  cloning.value = true
  try {
    const project = await store.cloneApp(slug)
    ElMessage.success('克隆成功，已复制到你的项目')
    router.push({ name: 'project', params: { id: project.id } })
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? e.detail : '克隆失败，请稍后重试')
  } finally {
    cloning.value = false
  }
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <el-container class="detail">
    <el-header class="header">
      <el-button text @click="router.push({ name: 'world' })">← App 世界</el-button>
      <span class="brand">Atoms Demo</span>
    </el-header>
    <el-main v-if="app" class="main">
      <div class="info-panel">
        <h2 class="title">{{ app.title }}</h2>
        <p class="description">{{ app.description || '暂无描述' }}</p>
        <div class="meta">
          <span>作者：{{ app.author }}</span>
          <span>发布于 {{ formatTime(app.published_at) }}</span>
        </div>
        <el-button
          type="primary"
          size="large"
          :loading="cloning"
          class="clone-btn"
          data-testid="clone-app"
          @click="onClone"
        >
          克隆到我的项目
        </el-button>
        <p class="clone-hint">
          克隆会把全部文件复制到你名下，成为独立的新项目，可以立即继续对话迭代；不会影响原应用与其公开链接。
        </p>
      </div>
      <!-- 实时运行预览：与公开链接同源的完整运行效果 -->
      <div class="preview-panel">
        <iframe
          :src="app.preview_url"
          class="preview-frame"
          sandbox="allow-scripts allow-forms allow-popups allow-modals"
        />
      </div>
    </el-main>
    <el-main v-else-if="notFound">
      <el-empty description="应用不存在或已下架">
        <el-button type="primary" @click="router.push({ name: 'world' })">回到 App 世界</el-button>
      </el-empty>
    </el-main>
  </el-container>
</template>

<style scoped>
.detail {
  height: 100%;
}

.header {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.brand {
  font-size: 18px;
  font-weight: 600;
}

.main {
  display: flex;
  gap: 16px;
  align-items: stretch;
}

.info-panel {
  width: 320px;
  flex-shrink: 0;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 20px;
}

.title {
  margin: 0;
  font-size: 20px;
}

.description {
  margin: 12px 0;
  color: #606266;
  font-size: 14px;
  line-height: 1.6;
  word-break: break-all;
}

.meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: #909399;
  font-size: 13px;
}

.clone-btn {
  width: 100%;
  margin-top: 20px;
}

.clone-hint {
  margin: 12px 0 0;
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
}

.preview-panel {
  flex: 1;
  min-width: 0;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  overflow: hidden;
}

.preview-frame {
  width: 100%;
  height: 100%;
  border: 0;
}
</style>
