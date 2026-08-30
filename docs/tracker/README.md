# 本地 Issue Tracker 约定

在运行 `/setup-matt-pocock-skills` 接入真实 tracker 之前，规格与工单暂存于此目录。

## 文件格式

每个工单一个 Markdown 文件，命名 `{4位序号}-{slug}.md`，头部 frontmatter：

```yaml
---
id: "0001"
title: 标题
labels: [ready-for-agent]
status: open   # open | in-progress | done | dropped
---
```

## 标签词汇表（Triage Labels）

| 标签 | 含义 |
|------|------|
| `ready-for-agent` | 规格完整、决策闭环，智能体可直接开工实现 |
| `needs-grilling` | 需求尚有模糊点，需先拷问澄清 |
| `blocked` | 依赖外部事实（如服务器、API Key）未就绪 |
