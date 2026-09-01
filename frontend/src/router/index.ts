import { createRouter, createWebHistory } from 'vue-router'

import { getToken } from '@/api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/workspace' },
    // 无独立项目列表页，裸路径重定向到工作区，避免直接访问时白屏/404 观感
    { path: '/projects', redirect: '/workspace' },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginPage.vue'),
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/RegisterPage.vue'),
    },
    {
      path: '/world',
      name: 'world',
      component: () => import('@/views/AppWorldPage.vue'),
    },
    {
      path: '/world/:slug',
      name: 'world-detail',
      component: () => import('@/views/AppDetailPage.vue'),
    },
    {
      path: '/workspace',
      name: 'workspace',
      component: () => import('@/views/WorkspacePage.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/projects/:id',
      name: 'project',
      component: () => import('@/views/ProjectPage.vue'),
      meta: { requiresAuth: true },
    },
  ],
})

router.beforeEach((to) => {
  if (to.meta.requiresAuth && !getToken()) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
})

export default router
