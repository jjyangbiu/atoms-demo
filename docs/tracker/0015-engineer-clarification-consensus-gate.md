---
id: "0015"
title: 工程师模式需求澄清与共识确认门
labels: [done]
status: done
---

# 0015 — 工程师模式需求澄清与共识确认门

**What to build:** 工程师模式新建项目的首条消息不再直接生成代码，而是进入需求澄清对话：澄清智能体分轮问答（每轮把所有会实质影响生成结果的未决问题合并为一条消息、每题附推荐答案），需求无未决问题时产出需求共识卡片；用户确认共识后工程师才开始生成。用户随时可说"直接生成"跳过澄清；共识待确认时继续发消息视为追加输入，重新澄清并产出新共识。整个首建流水线（澄清到生成完）只占 1 个生成名额。遵循 ADR 0003。

**Blocked by:** 无 — 可立即开工

**Status:** done

- [x] 首条消息进入澄清问答而非生成代码，问答全程落对话历史，刷新可回看（test_clarification.py::TestClarifyRouting::test_first_message_asks_questions_instead_of_generating）
- [x] 澄清智能体无法调用文件工具，唯一出口是携带需求摘要的 start_build（test_clarifier_cannot_use_file_tools）
- [x] 用户明确说"直接生成/跳过澄清"时立即进入生成（test_escape_hatch_generates_directly；官方示例灌入亦经此逃生门收敛）
- [x] 需求共识以卡片呈现，确认后（SSE）触发工程师生成；待确认时发消息则重新澄清、新共识取代旧共识（TestConsensusGate 全组）
- [x] 首建流水线整体只扣 1 个名额；生成完成后的迭代消息恢复按次计数（TestQuotaSemantics::test_first_build_pipeline_consumes_single_quota）
- [x] 已有文件的项目（含克隆）不触发澄清，行为与现状一致（TestExistingFilesSkipClarify；克隆可立即迭代由 test_world 回归）
- [x] 测试使用 fake 模型，覆盖分流、逃生门、确认门与名额语义（全套 128 项回归通过，无任何真实 MiniMax 调用）
