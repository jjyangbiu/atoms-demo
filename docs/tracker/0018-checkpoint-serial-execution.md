---
id: "0018"
title: 工程师按检查点串行执行工单
labels: [done]
status: done
---

# 0018 — 工程师按检查点串行执行工单

**What to build:** 工单清单确认后，工程师智能体按阻塞依赖顺序逐个执行工单（一次只执行一个，完成一个再进入下一个）。每个工单执行完成后形成一个检查点快照，融入现有版本快照体系（代码视图、预览、回滚入口直接可用）。SSE 推送工单进度事件（开始 / 完成 / 失败），前端展示执行中的工单与整体进度。工单执行失败可从该工单起点重试；断线重连与重试都不重复占用名额——整个首建流水线仍只占 1 个名额。遵循 ADR 0003。

**Blocked by:** 0017（没有已确认的工单清单无从执行）

**Status:** done

- [x] 工单按依赖顺序串行执行，前端实时展示当前工单与进度（test_team_exec.py::TestSerialExecution::test_tickets_execute_serially_with_progress_events；SSE 新增 ticket_progress 事件，前端清单卡片逐单状态标签 + 进度条）
- [x] 每个工单完成形成检查点快照，可在快照列表查看、预览（Ticket 新增 snapshot_id 引用，完成与快照同一事务写入；test_each_ticket_forms_checkpoint_snapshot）
- [x] 单个工单失败可重试，不从第一个工单重来（失败标 failed 后终止，/tickets/resume 从第一张未完成工单起点继续；test_failure_stops_at_ticket_and_retry_from_it 验证已完成单未重跑）
- [x] 断线重连后可看到执行状态并继续；全流程不重复扣名额（执行状态在工单表 + 进度行入对话历史，刷新重建；/tickets/resume 不占名额；test_execution_and_retry_share_single_quota）
- [x] 全部工单完成后项目进入常规迭代体验（迭代消息按次计数；_exec_state 分流，执行期普通消息引导拦截不计数）
- [x] 测试使用 fake 模型，覆盖串行顺序、检查点生成、失败重试与名额语义（test_team_exec.py 8 个；适配 test_team_tickets.py 5 处，共 157 全绿）
