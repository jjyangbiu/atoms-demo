<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

onMounted(() => {
  auth.fetchMe()
})

async function onLogout() {
  await auth.logout()
  router.push('/login')
}
</script>

<template>
  <el-container class="workspace">
    <el-header class="header">
      <span class="brand">Atoms Demo</span>
      <div class="header-right">
        <span class="username">{{ auth.user?.username }}</span>
        <el-button text @click="onLogout">退出登录</el-button>
      </div>
    </el-header>
    <el-main>
      <el-empty description="还没有项目——项目创建能力将在下个工单交付" />
    </el-main>
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
</style>
