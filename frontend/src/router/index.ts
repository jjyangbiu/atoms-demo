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
      path: '/workspace',
      name: 'workspace',
      component: () => import('@/views/WorkspacePage.vue'),
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
