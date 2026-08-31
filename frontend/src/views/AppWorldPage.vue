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
  <el-container class="world">
    <el-header class="header">
      <span class="brand">Atoms Demo</span>
      <span class="page-title">App 世界</span>
      <div class="header-right">
        <el-button v-if="loggedIn" text @click="router.push({ name: 'workspace' })">
          我的工作台
        </el-button>
        <el-button v-else text type="primary" @click="router.push({ name: 'login' })">
          登录 / 注册
        </el-button>
      </div>
    </el-header>
    <el-main>
      <p class="intro">所有已发布的应用都在这里：点开试用，喜欢就克隆一份继续迭代。</p>
      <el-input
        v-model="keyword"
        class="search"
        placeholder="按意图搜索应用，如“记账工具”“倒计时”"
        clearable
        data-testid="world-search"
        @keyup.enter="reload"
        @clear="reload"
      >
        <template #append>
          <el-button data-testid="world-search-btn" @click="reload">搜索</el-button>
        </template>
      </el-input>

      <el-empty
        v-if="!loading && store.apps.length === 0"
        :description="keyword.trim() ? '没有找到相关应用，换个说法试试' : '还没有已发布的应用'"
      />

      <div v-else class="app-grid">
        <el-card
          v-for="app in store.apps"
          :key="app.slug"
          class="app-card"
          shadow="hover"
          :body-style="{ padding: '0' }"
          :data-testid="`world-card-${app.slug}`"
          @click="router.push({ name: 'world-detail', params: { slug: app.slug } })"
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
              {{ app.title }}
              <!-- 官方示例标识（工单 0012）：系统自身链路生成的画廊冷启动示例 -->
              <el-tag v-if="app.official" size="small" type="warning" effect="plain">
                官方示例
              </el-tag>
            </div>
            <div class="card-desc">{{ app.description || '暂无描述' }}</div>
            <div class="card-meta">
              <span>{{ app.author }}</span>
              <span>发布于 {{ formatTime(app.published_at) }}</span>
            </div>
          </div>
        </el-card>
      </div>
    </el-main>
  </el-container>
</template>

<style scoped>
.world {
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

.page-title {
  color: #909399;
  font-size: 14px;
}

.header-right {
  margin-left: auto;
}

.intro {
  margin: 0 0 16px;
  color: #606266;
  font-size: 13px;
}

.search {
  max-width: 480px;
  margin-bottom: 16px;
}

.app-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}

.app-card {
  cursor: pointer;
  overflow: hidden;
}

.thumb {
  position: relative;
  width: 100%;
  height: 160px;
  overflow: hidden;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
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
  padding: 12px;
}

.card-title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-title .el-tag {
  margin-left: 6px;
  vertical-align: 1px;
}

.card-desc {
  margin-top: 6px;
  color: #606266;
  font-size: 13px;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}

.card-meta {
  margin-top: 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #909399;
  font-size: 12px;
}
</style>
