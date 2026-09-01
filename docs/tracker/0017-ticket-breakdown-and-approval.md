---
id: "0017"
title: 工单拆解与工单清单确认门
labels: [done]
status: done
---

# 0017 — 工单拆解与工单清单确认门

**What to build:** 团队模式需求规格确认后，拆单智能体把规格拆解为纵向切片工单：每个工单是可独立预览的完整路径，数量控制在个位数，声明工单间阻塞依赖。工单清单以卡片呈现（标题 / 交付内容 / 被谁阻塞 / 状态），用户确认后才允许执行；待确认时继续发消息视为调整粒度或内容的意见，重新拆解并取代旧清单。工单一经确认进入执行期，不可重新澄清、不可重新拆单。工单持久化于新的工单表。遵循 ADR 0003。

**Blocked by:** 0016（规格确认是拆单的前置）

**Status:** done

- [x] 规格确认后自动拆单，工单清单卡片展示标题、交付内容与阻塞依赖（test_team_tickets.py::TestTicketBreakdown::test_spec_confirm_auto_breaks_down_tickets；前端 ProjectPage.vue 工单清单卡片）
- [x] 待确认时发消息重新拆解，新清单取代旧清单（test_team_tickets.py::TestTicketRedraft::test_message_during_pending_rebreaks_and_supersedes：旧待确认工单整批删除、序号续编）
- [x] 工单清单确认接口可用；确认后再发消息不触发重新澄清或拆单（test_team_tickets.py::TestTicketsConfirm::test_confirm_starts_engineer_with_tickets_in_context 与 test_message_after_confirmed_no_rebreak_no_reclarify）
- [x] 工单持久化于工单表（项目、序号、标题、交付内容、阻塞依赖、状态），刷新可回看（models.py Ticket 表 + GET /tickets；test_tickets_persisted_and_listed）
- [x] 测试使用 fake 模型，覆盖拆单、待确认分流与确认后冻结（test_team_tickets.py 11 例 + test_team_spec.py 适配，共 149 全绿）

