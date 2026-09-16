---
id: "0029"
title: 团队工单零交付闸（零改动收尾不得静默标 done）
labels: [done]
status: done
---

# 0029 — 团队工单零交付闸（零改动收尾不得静默标 done）

**What to build:** 团队工单执行轮以「磁盘零改动」收尾时，现状两条出口（散文兜底、结构化 no_change）都直接标 done 并建检查点——工单的成功信号建立在零交付上，模型「说改好了但没改」可以畅通关单，done 是持久状态、resume 会跳过。本工单为工单执行轮加零交付闸（`_engineer_stream` 新增 `require_deliverable`，仅 `_exec_tickets_stream` 传 True）：磁盘零改动的轮次只有一条合法出口——经工单级确认回喂一次后的结构化 no_change（覆盖「前次执行中断、交付已在磁盘」的重跑轮，零 diff 闸禁止空编辑硬凑改动，不能一刀切失败否则该单永远关不掉）；该出口照常 done + 检查点，但进度行显式标注「零改动收尾」交人工判断。其余零改动出口（散文兜底收尾、modify_code 申报零改动经 verbal_completion 放行）一律按执行失败处置：不建快照、不建卡片、不标 done，工单标 failed，/tickets/resume 从该单重试。判据全部来自磁盘指纹比对（工单 0022 权威来源），不采信任何模型申报。用户对话轮与确认驱动的流水线轮不受影响（require_deliverable=False）。

**Blocked by:** 0018（串行执行与失败重试语义）、0025（自洽性核验的回喂范式与轮级预算房规）

**Status:** done

- [x] 工单执行轮的结构化 no_change 且磁盘零改动 → 工单级确认回喂一次（独立于自洽性回喂的轮级预算；文案给出两条出路：继续交付，或说明磁盘现状已满足交付内容后重新提交）
- [x] 回喂后第二次提交仍 no_change → 照常 done + 检查点快照（0018 语义不变），result 附 zero_change，done 进度事件与落库 payload 携可选字段 zero_change: true
- [x] 前端进度行渲染「（零改动收尾）」标注（ticketProgressText 与历史 kind=ticket 行共用同一口径，实时与回看一致）
- [x] 散文兜底收尾且磁盘零改动 → 不建快照、不建卡片、不标 done：result.ok=False + error「零改动收尾，未确认交付」，工单标 failed，/tickets/resume 从该单重试
- [x] modify_code 申报零改动（自洽性回喂后仍申报，verbal_completion 放行）→ 同上按失败处置
- [x] 判据只看磁盘指纹比对结果，不采信模型申报；用户对话轮、确认驱动流水线轮、克隆项目直接实现轮行为零变化
- [x] 测试使用 fake 模型经 HTTP 接缝覆盖：散文兜底零改动 failed + resume 重试成功、口头完成零改动 failed + resume、确认后 no_change done + 进度事件与落库行携 zero_change 标注、确认回喂文案确实到达模型（test_team_exec.py::TestZeroDeliverableGate 3 个；适配 test_team_spec.py 2 处——原脚本靠脚本耗尽的兜底零改动关闭工单 2，正是本闸堵住的洞）
