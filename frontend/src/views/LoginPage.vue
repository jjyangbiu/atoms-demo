<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const form = reactive({ username: '', password: '' })
const loading = ref(false)

async function onSubmit() {
  if (!form.username || !form.password) return
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/workspace'
    router.push(redirect)
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? e.detail : '登录失败，请稍后重试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <el-card class="auth-card">
      <h1 class="brand">Atoms Demo</h1>
      <p class="subtitle">智能体驱动的应用生成平台</p>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            placeholder="请输入用户名"
            autocomplete="username"
            data-testid="login-username"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            show-password
            data-testid="login-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button
          type="primary"
          :loading="loading"
          class="submit-btn"
          data-testid="login-submit"
          @click="onSubmit"
        >
          登录
        </el-button>
      </el-form>
      <p class="switch-link">
        还没有账号？
        <router-link to="/register">立即注册</router-link>
      </p>
      <p class="switch-link">
        <router-link to="/world">先逛逛 App 世界，看看大家构建的应用</router-link>
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.auth-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.auth-card {
  width: 380px;
  padding: 8px 4px;
}

.brand {
  margin: 0;
  text-align: center;
  font-size: 24px;
}

.subtitle {
  margin: 6px 0 24px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}

.submit-btn {
  width: 100%;
}

.switch-link {
  margin-top: 16px;
  text-align: center;
  font-size: 13px;
  color: #909399;
}
</style>
