<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { getToken } from '@/api/client'
import { useWorldStore } from '@/stores/world'

const store = useWorldStore()
const router = useRouter()

const loading = ref(true)
const loggedIn = ref(false)
// 语义搜索关键词（工单 0009）：按意图命中相关应用，非关键词精确匹配
const keyword = ref('')

onMounted(async () => {
  loggedIn.value = getToken() !== null
  await reload()
})

async function reload(): Promise<void> {
  loading.value = true
  try {
    await store.fetchWorld(keyword.value)
  } finally {
    loading.value = false
  }
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <div class="world">
    <header class="header">
      <span class="brand brand-mark">Atoms Demo</span>
      <span class="page-title">App 世界</span>
      <div class="header-right">
        <el-button v-if="loggedIn" text @click="router.push({ name: 'workspace' })">
          我的工作台
        </el-button>
        <el-button v-else type="primary" round @click="router.push({ name: 'login' })">
          登录 / 注册
        </el-button>
      </div>
    </header>

    <main class="main">
      <section class="hero">
        <h1 class="hero-title">App 世界</h1>
        <p class="hero-sub">所有已发布的应用都在这里：点开试用，喜欢就克隆一份继续迭代。</p>
        <div class="search-row">
          <el-input
            v-model="keyword"
            class="search"
            size="large"
            placeholder="按意图搜索应用，如“记账工具”“倒计时”"
            clearable
            data-testid="world-search"
            @keyup.enter="reload"
            @clear="reload"
          >
            <template #prefix>
              <!-- 搜索图标（装饰性） -->
              <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
                <circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.6" />
                <path d="M11 11l3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              </svg>
            </template>
          </el-input>
          <el-button
            type="primary"
            size="large"
            class="search-btn"
            data-testid="world-search-btn"
            @click="reload"
          >
            搜索
          </el-button>
        </div>
      </section>

      <el-empty
        v-if="!loading && store.apps.length === 0"
        :description="keyword.trim() ? '没有找到相关应用，换个说法试试' : '还没有已发布的应用'"
      />

      <div v-else class="app-grid">
        <div
          v-for="app in store.apps"
          :key="app.slug"
          class="app-card"
          role="button"
          tabindex="0"
          :data-testid="`world-card-${app.slug}`"
          :aria-label="`查看应用 ${app.title}`"
          @click="router.push({ name: 'world-detail', params: { slug: app.slug } })"
          @keydown.enter="router.push({ name: 'world-detail', params: { slug: app.slug } })"
        >
          <!-- 缩略预览：禁用交互让整卡可点，脚本仍运行以呈现真实效果 -->
          <div class="thumb">
            <iframe
              :src="app.preview_url"
              class="thumb-frame"
              sandbox="allow-scripts"
              loading="lazy"
              tabindex="-1"
            />
          </div>
          <div class="card-info">
            <div class="card-title">
              <span class="card-title-text">{{ app.title }}</span>
              <!-- 官方示例标识（工单 0012）：系统自身链路生成的画廊冷启动示例 -->
              <el-tag v-if="app.official" size="small" type="warning" effect="plain">
                官方示例
              </el-tag>
            </div>
            <div class="card-desc">{{ app.description || '暂无描述' }}</div>
            <div class="card-meta">
              <span class="meta-author">
                <span class="author-dot" aria-hidden="true">{{ app.author.charAt(0) }}</span>
                {{ app.author }}
              </span>
              <span>{{ formatTime(app.published_at) }}</span>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
.world {
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

.brand {
  font-size: 17px;
}

.page-title {
  padding: 2px 10px;
  border-radius: var(--at-radius-full);
  background: var(--at-accent-soft);
  border: 1px solid #bae6fd;
  color: var(--at-accent);
  font-size: 12px;
  font-weight: 600;
}

.header-right {
  margin-left: auto;
}

/* ---- 主区与搜索英雄区 ---- */
.main {
  flex: 1;
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: var(--at-space-6) var(--at-space-5) var(--at-space-7);
}

.hero {
  text-align: center;
  padding: var(--at-space-5) 0 var(--at-space-6);
}

.hero-title {
  margin: 0;
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -0.02em;
  background: var(--at-gradient-brand);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: var(--at-primary);
}

.hero-sub {
  margin: 8px 0 0;
  color: var(--at-text-muted);
  font-size: 14px;
}

.search-row {
  display: flex;
  justify-content: center;
  gap: var(--at-space-2);
  margin-top: var(--at-space-5);
}

.search {
  width: 480px;
  max-width: 100%;
}

.search :deep(.el-input__wrapper) {
  border-radius: var(--at-radius-md);
  background: var(--at-card);
  box-shadow: 0 0 0 1px var(--at-border-strong) inset, var(--at-shadow-sm);
}

.search-btn {
  border-radius: var(--at-radius-md);
  font-weight: 600;
}

/* ---- 应用卡片网格 ---- */
.app-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--at-space-5);
}

.app-card {
  cursor: pointer;
  overflow: hidden;
  background: var(--at-card);
  border: 1px solid var(--at-border);
  border-radius: var(--at-radius-lg);
  transition:
    transform var(--at-duration) var(--at-ease),
    box-shadow var(--at-duration) var(--at-ease),
    border-color var(--at-duration) var(--at-ease);
}

.app-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--at-shadow-lg);
  border-color: var(--at-primary-border);
}

.thumb {
  position: relative;
  width: 100%;
  height: 170px;
  overflow: hidden;
  background: var(--at-bg-deep);
  border-bottom: 1px solid var(--at-border);
}

.thumb-frame {
  width: 200%;
  height: 200%;
  border: 0;
  transform: scale(0.5);
  transform-origin: top left;
  /* 缩略图只展示不交互：点击落在卡片上进入详情页 */
  pointer-events: none;
}

.card-info {
  padding: 14px 16px 16px;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 15px;
  color: var(--at-text);
}

.card-title-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-desc {
  margin-top: 6px;
  color: var(--at-text-secondary);
  font-size: 13px;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}

.card-meta {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--at-text-muted);
  font-size: 12px;
}

.meta-author {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.author-dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--at-primary-soft);
  border: 1px solid var(--at-primary-border);
  color: var(--at-primary);
  font-size: 11px;
  font-weight: 600;
}

/* ---- 窄屏适配 ---- */
@media (max-width: 640px) {
  .header {
    padding: 0 var(--at-space-4);
  }

  .main {
    padding: var(--at-space-4) var(--at-space-4) var(--at-space-6);
  }

  .hero-title {
    font-size: 24px;
  }

  .search-row {
    flex-direction: column;
    align-items: stretch;
  }

  .search {
    width: 100%;
  }
}
</style>
