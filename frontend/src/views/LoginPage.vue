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
    <!-- 氛围背景光斑：纯装饰，对读屏隐藏 -->
    <div class="glow glow-a" aria-hidden="true"></div>
    <div class="glow glow-b" aria-hidden="true"></div>

    <div class="auth-card">
      <h1 class="brand brand-mark">Atoms Demo</h1>
      <p class="subtitle">智能体驱动的应用生成平台</p>
      <p class="tagline">描述你的想法，智能体为你生成、迭代并发布应用</p>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            size="large"
            placeholder="请输入用户名"
            autocomplete="username"
            data-testid="login-username"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            size="large"
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
          size="large"
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
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  position: relative;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--at-space-4);
  background: var(--at-gradient-soft);
  overflow: hidden;
}

/* 品牌氛围光斑 */
.glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(90px);
  pointer-events: none;
}

.glow-a {
  width: 480px;
  height: 480px;
  top: -140px;
  left: -120px;
  background: rgba(124, 58, 237, 0.18);
}

.glow-b {
  width: 420px;
  height: 420px;
  bottom: -140px;
  right: -100px;
  background: rgba(8, 145, 178, 0.14);
}

.auth-card {
  position: relative;
  z-index: 1;
  width: 420px;
  max-width: 100%;
  padding: 36px 36px 28px;
  background: rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.9);
  border-radius: var(--at-radius-lg);
  box-shadow: var(--at-shadow-lg);
}

.brand {
  margin: 0;
  justify-content: center;
  font-size: 26px;
}

.brand::before {
  width: 28px;
  height: 28px;
  border-radius: 9px;
}

.subtitle {
  margin: 10px 0 0;
  text-align: center;
  color: var(--at-text-secondary);
  font-size: 14px;
  font-weight: 500;
}

.tagline {
  margin: 4px 0 28px;
  text-align: center;
  color: var(--at-text-muted);
  font-size: 12px;
}

.submit-btn {
  width: 100%;
  margin-top: 4px;
  font-weight: 600;
  background: var(--at-gradient-brand);
  border: none;
}

.submit-btn:hover {
  opacity: 0.92;
}

.switch-link {
  margin-top: 16px;
  text-align: center;
  font-size: 13px;
  color: var(--at-text-muted);
}

.switch-link a {
  color: var(--at-primary);
  text-decoration: none;
  font-weight: 500;
}

.switch-link a:hover {
  text-decoration: underline;
}
</style>
