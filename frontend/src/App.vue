<script setup lang="ts"></script>

<template>
  <router-view />
</template>

<style>
/* 基础样式已收敛到 src/styles/base.css（设计令牌 + Element Plus 主题覆盖）。
   这里只保留跨页面复用的品牌标识样式。 */
.brand-mark {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-weight: 700;
  color: var(--at-text);
  letter-spacing: -0.01em;
}

.brand-mark::before {
  content: '';
  width: 22px;
  height: 22px;
  border-radius: 7px;
  background: var(--at-gradient-brand);
  box-shadow: var(--at-shadow-glow);
  flex: none;
}
</style>
