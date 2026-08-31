import { createRouter, createWebHistory } from 'vue-router'

import { getToken } from '@/api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/workspace' },
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
