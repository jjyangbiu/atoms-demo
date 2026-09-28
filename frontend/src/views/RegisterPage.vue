<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const form = reactive({ username: '', password: '', confirm: '' })
const loading = ref(false)

async function onSubmit() {
  if (!form.username || !form.password) return
  if (form.password.length < 6) {
    ElMessage.error('密码至少 6 位')
    return
  }
  if (form.password !== form.confirm) {
    ElMessage.error('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    await auth.register(form.username, form.password)
    ElMessage.success('注册成功，已自动登录')
    router.push('/workspace')
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? e.detail : '注册失败，请稍后重试')
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
      <p class="subtitle">创建账号，开始构建你的应用</p>
      <p class="tagline">注册后即可创建项目、对话生成并一键发布</p>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            size="large"
            placeholder="2~32 位字母、数字、下划线或中文"
            autocomplete="username"
            data-testid="register-username"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            size="large"
            type="password"
            placeholder="至少 6 位"
            autocomplete="new-password"
            show-password
            data-testid="register-password"
          />
        </el-form-item>
        <el-form-item label="确认密码">
          <el-input
            v-model="form.confirm"
            size="large"
            type="password"
            placeholder="再次输入密码"
            autocomplete="new-password"
            show-password
            data-testid="register-confirm"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          :loading="loading"
          class="submit-btn"
          data-testid="register-submit"
          @click="onSubmit"
        >
          注册
        </el-button>
      </el-form>
      <p class="switch-link">
        已有账号？
        <router-link to="/login">去登录</router-link>
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
  right: -120px;
  background: rgba(8, 145, 178, 0.16);
}

.glow-b {
  width: 420px;
  height: 420px;
  bottom: -140px;
  left: -100px;
  background: rgba(124, 58, 237, 0.18);
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
  margin: 4px 0 24px;
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
