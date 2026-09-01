---
id: "0016"
title: 团队模式澄清后产需求规格（PRD 退役）
labels: [done]
status: done
---

# 0016 — 团队模式澄清后产需求规格（PRD 退役）

**What to build:** 团队模式新建项目在需求澄清收敛后，由规格智能体产出结构化需求规格（目标 / 功能清单 / 页面与交互 / 视觉风格 / 不做的事，面向终端用户、不含工程段落），渲染为带确认操作的卡片；用户确认后流水线进入下一阶段入口（拆单由工单 0017 承接）。规格待确认时继续发消息视为修改意见，重新起草规格。旧"产 PRD"流程对新团队项目退役，历史项目的 PRD 展示、确认与只读路径保持兼容。遵循 ADR 0003。

**Blocked by:** 0015（复用澄清阶段与确认门机制）

**Status:** done

- [x] 团队模式澄清收敛后产出需求规格卡片，确认操作可用（test_team_spec.py::TestSpecProduction::test_consensus_confirm_streams_spec_card_and_writes_no_files；TestSpecConfirm::test_confirm_starts_engineer_with_spec_in_context——确认后暂由工程师实现承接下一阶段入口，拆单由工单 0017 替换）
- [x] 规格待确认时发消息重新起草，新规格取代旧规格（TestSpecRedraft::test_message_during_pending_redrafts_and_supersedes；旧规格入重起草上下文）
- [x] 新团队项目不再产出 PRD；历史团队项目的 PRD 流程不受影响（TestSpecProduction::test_new_team_project_first_message_goes_to_clarify_not_prd；PM 产 PRD 生成路径随退役移除，_pm_stream/PM_SYSTEM_PROMPT 不再存在；历史项目经"已有 PRD 消息"判定保留引导/确认/只读路径——TestLegacyPrdCompatibility 全组与 test_team_mode 兼容回归）
- [x] 规格、确认消息持久化于对话历史，刷新可回看（TestSpecProduction::test_spec_persisted_in_history；spec/spec_confirm 沿用消息 kind 模式，阶段状态从历史推导不加表字段；断流不残留半截规格——test_refresh_state_loss::test_refresh_midstream_spec_thinking_survives）
- [x] 测试使用 fake 模型，覆盖规格产出、待确认分流与历史兼容（新增 test_team_spec.py 13 个用例，含名额语义"一次名额管到底"；全套 138 项回归通过，前端 vue-tsc 与构建通过）
