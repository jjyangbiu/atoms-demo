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
    <el-card class="auth-card">
      <h1 class="brand">Atoms Demo</h1>
      <p class="subtitle">创建账号，开始构建你的应用</p>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            placeholder="2~32 位字母、数字、下划线或中文"
            autocomplete="username"
            data-testid="register-username"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
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
