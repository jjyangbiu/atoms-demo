---
id: "0013"
title: Docker Compose 本地编排
labels: [ready-for-agent]
status: open
---

# 13 — Docker Compose 本地编排

**What to build:** 一条 `docker compose up` 在本地拉起完整系统：nginx（前端静态 + `/p/` 公开静态 + `/preview/` 代理鉴权 + 反代 `/api`）+ FastAPI（uvicorn）+ 数据卷（SQLite、Milvus Lite 数据、生成文件）。这是上线工单的预演，环境配置（API Key、限流参数）全部经环境变量注入。

**Blocked by:** 06（发布与稳定链接）

**Status:** ready-for-agent

- [ ] `docker compose up` 后：注册登录、生成、预览、公开链接全部经容器链路可用
- [ ] `/p/` 静态由 nginx 直出，`/preview/` 经鉴权代理，`/api` 反代正常
- [ ] 容器重启后数据（账号、项目、发布、知识库）不丢失
- [ ] 环境变量文档齐全：API Key、模型配置、限流参数、存储路径
