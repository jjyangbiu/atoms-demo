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
  <div class="detail">
    <header class="header">
      <el-button text class="back-btn" @click="router.push({ name: 'world' })">
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
          <path
            d="M10 3L5 8l5 5"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        App 世界
      </el-button>
      <span class="brand brand-mark">Atoms Demo</span>
    </header>

    <main v-if="app" class="main">
      <aside class="info-panel">
        <div class="info-head">
          <h2 class="title">{{ app.title }}</h2>
          <!-- 官方示例标识（工单 0012） -->
          <el-tag v-if="app.official" size="small" type="warning" effect="plain">官方示例</el-tag>
        </div>
        <p class="description">{{ app.description || '暂无描述' }}</p>
        <div class="meta">
          <span class="meta-author">
            <span class="author-dot" aria-hidden="true">{{ app.author.charAt(0) }}</span>
            {{ app.author }}
          </span>
          <span class="meta-time">发布于 {{ formatTime(app.published_at) }}</span>
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
      </aside>
      <!-- 实时运行预览：与公开链接同源的完整运行效果 -->
      <section class="preview-panel" aria-label="应用实时预览">
        <iframe
          :src="app.preview_url"
          class="preview-frame"
          sandbox="allow-scripts allow-forms allow-popups allow-modals"
          title="应用预览"
        />
      </section>
    </main>
    <main v-else-if="notFound" class="main">
      <div class="notfound-card">
        <el-empty description="应用不存在或已下架">
          <el-button type="primary" @click="router.push({ name: 'world' })">回到 App 世界</el-button>
        </el-empty>
      </div>
    </main>
  </div>
</template>

<style scoped>
.detail {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--at-bg);
}

/* ---- 顶栏 ---- */
.header {
  display: flex;
  align-items: center;
  gap: var(--at-space-3);
  height: 60px;
  padding: 0 var(--at-space-5);
  background: rgba(255, 255, 255, 0.8);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--at-border);
  position: sticky;
  top: 0;
  z-index: 10;
}

.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--at-text-secondary);
  font-weight: 500;
}

.back-btn:hover {
  color: var(--at-primary);
}

.brand {
  font-size: 17px;
  margin-left: auto;
}

/* ---- 主区：左信息栏 + 右实时预览 ---- */
.main {
  flex: 1;
  display: flex;
  gap: var(--at-space-4);
  align-items: stretch;
  width: 100%;
  max-width: 1400px;
  margin: 0 auto;
  padding: var(--at-space-4) var(--at-space-5) var(--at-space-5);
  min-height: 0;
}

.info-panel {
  width: 340px;
  flex-shrink: 0;
  background: var(--at-card);
  border: 1px solid var(--at-border);
  border-radius: var(--at-radius-lg);
  padding: var(--at-space-5);
  box-shadow: var(--at-shadow-sm);
  overflow-y: auto;
}

.info-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--at-space-2);
}

.title {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--at-text);
  word-break: break-word;
}

.description {
  margin: 12px 0;
  color: var(--at-text-secondary);
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}

.meta {
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: var(--at-text-muted);
  font-size: 13px;
  padding-top: 12px;
  border-top: 1px dashed var(--at-border);
}

.meta-author {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.author-dot {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--at-gradient-brand);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}

.clone-btn {
  width: 100%;
  margin-top: var(--at-space-5);
  font-weight: 600;
  background: var(--at-gradient-brand);
  border: none;
}

.clone-hint {
  margin: 12px 0 0;
  padding: 10px 12px;
  color: var(--at-text-secondary);
  font-size: 12px;
  line-height: 1.7;
  background: var(--at-primary-soft);
  border: 1px solid var(--at-primary-border);
  border-radius: var(--at-radius-sm);
}

.preview-panel {
  flex: 1;
  min-width: 0;
  background: var(--at-card);
  border: 1px solid var(--at-border);
  border-radius: var(--at-radius-lg);
  overflow: hidden;
  box-shadow: var(--at-shadow-md);
}

.preview-frame {
  width: 100%;
  height: 100%;
  border: 0;
}

.notfound-card {
  margin: auto;
  background: var(--at-card);
  border: 1px solid var(--at-border);
  border-radius: var(--at-radius-lg);
  padding: var(--at-space-7) var(--at-space-7);
}

/* ---- 窄屏适配：信息栏叠在预览上方 ---- */
@media (max-width: 900px) {
  .main {
    flex-direction: column;
    overflow-y: auto;
    padding: var(--at-space-4);
  }

  .info-panel {
    width: 100%;
    overflow-y: visible;
  }

  .preview-panel {
    min-height: 60vh;
  }
}
</style>
