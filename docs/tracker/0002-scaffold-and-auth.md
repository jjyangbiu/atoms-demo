---
id: "0002"
title: 工程脚手架 + 注册登录
labels: [ready-for-agent]
status: open
---

# 02 — 工程脚手架 + 注册登录

**What to build:** 从零立起前后端与测试骨架，并让用户在浏览器里完成注册、登录、登出——这是之后所有垂直切片的地基。

后端：FastAPI 应用骨架（配置走环境变量）、SQLite 接入、用户表、注册/登录/登出/当前用户接口（密码哈希 + JWT）。前端：Vue 3 + Vite + TypeScript + Element Plus + Pinia + Vue Router 骨架，登录/注册页与登录态守卫。测试：pytest + TestClient + 临时 SQLite/存储目录的公共 fixture（这是后续所有测试的范本）。

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] 用户名+密码注册成功后可登录；重复用户名、错误凭证均有明确错误提示
- [ ] 登录态经 JWT 维持，过期/无效令牌访问受保护端点返回 401
- [ ] 前端注册/登录页可用，登录后进入工作台空态页面，可登出
- [ ] 测试骨架（临时目录、测试客户端、认证辅助）就位并有首个端到端鉴权测试通过
