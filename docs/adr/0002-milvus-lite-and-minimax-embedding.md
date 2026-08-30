# ADR 0002：向量库选用 Milvus Lite，embedding 采用 MiniMax embo-01

- 状态：已接受（2026-08-30，经需求拷问会确认）

## 背景

平台有两处向量检索需求：生成前的模板知识库检索增强（RAG），以及 App 世界对已发布应用的语义搜索。候选嵌入式向量库：Milvus Lite（用户最初指定）、Chroma（更轻、API 更简单）、sqlite-vec（与主库 SQLite 同库）。

## 决策

1. 向量库采用 **Milvus Lite**（`pymilvus` 嵌入式运行，无独立服务进程）。
2. embedding 采用 **MiniMax `embo-01`**（经 `langchain_community.MiniMaxEmbeddings`），与主生成模型同属一个 API 体系，只管理一套 Key。
3. 服务器（待购）建议不低于 2C4G，为 Milvus Lite 留出内存余量。

## 被否决的替代方案

- **Chroma**：内存占用更小、集成同样成熟，但放弃用户指定的技术栈收益不大；若实际部署内存不足可作为回退方案。
- **sqlite-vec**：零额外依赖，但 LangChain 生态支持较新，踩坑风险与工期不匹配。
- **本地句向量模型（如 bge 系列）**：免 API 成本，但引入模型下载与推理开销，且与"全链路走 MiniMax"的简洁性冲突。

## 后果

- 正面：向量库、LLM、embedding、部署全链路生态统一；Milvus Lite 免运维。
- 代价：内存敏感——若最终购入的 ECS 内存偏小导致吃力，回退路径是切换 Chroma（数据量小，重建索引成本低）；`embo-01` 与本地模型的向量空间不兼容，换 embedding 必须全量重建索引。
