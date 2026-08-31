---
id: "0012"
title: 官方示例冷启动
labels: [done]
status: done
---

# 12 — 官方示例冷启动

**What to build:** 用系统自身链路生成 3~5 个官方示例应用（待办清单、数据仪表盘、小游戏、落地页）并发布进 App 世界、标记"官方示例"——画廊不再空，克隆当场可演示；此过程同时是对全链路（生成→迭代→发布→画廊）的真实回归。

**Blocked by:** 06（发布与稳定链接）、09（模板知识库 RAG + 画廊语义搜索）

**Status:** done

- [x] 3~5 个官方示例应用经真实智能体链路生成并发布，画廊可见、可打开运行
- [x] 官方示例在画廊中有明确标识，且可被克隆
- [x] 示例生成过程中暴露的问题被记录并修复（全链路回归作用）
- [x] 示例生成脚本/流程可重复执行（新环境部署后可重建画廊种子）

**实现与回归记录（done 时回填）：**

- `backend/app/official_samples.py` 定义 4 个示例规格（待办清单/数据仪表盘/贪吃蛇小游戏/产品落地页）与灌入函数；`Publication.official` 标记，画廊接口与前端卡片/详情页展示“官方示例”。
- `backend/scripts/seed_official_samples.py` 可重复执行：已灌入跳过、未发布中断现场补齐、单个失败不阻断；旧库缺 `official` 列时自动补列。
- 全链路回归含迭代环节：每个规格带迭代诉求，新建示例走“首轮生成 → 一轮迭代 → 发布 → 匿名访问公开页校验可打开”；迭代失败不发布，重跑可重建。
- 真实链路回归暴露并修复的问题：
  1. 模型自发引入外部 CDN（tailwind/Google 字体）：诉求统一追加“全部内联、禁用外部资源”约束，重灌后 4 个示例均自包含。
  2. `jwt_secret` 默认值仅 20 字节，PyJWT 报 InsecureKeyLengthWarning：默认值加长至 32+ 字节。
  3. 生产 embedding 工厂用 `langchain_community.MiniMaxEmbeddings`，构建即失败（强依赖 MINIMAX_GROUP_ID），导致知识库与画廊语义搜索在生产静默降级：改走 OpenAI 兼容端点（新增 `ATOMS_EMBEDDING_BASE_URL` / `ATOMS_EMBEDDING_API_KEY`，兼容 `provider:model` 前缀），修复后语义搜索命中分 0.5+；ADR 0002 已加修订说明，实现类相应改名 `OpenAICompatibleEmbedder`。
