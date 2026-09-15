"""智能体系统提示。"""

from .verdicts import (
    CONSISTENT,
    FALLBACK,
    FILE_SET_MISMATCH,
    MISMATCH,
    UNDECLARED_CHANGE,
    VERBAL_COMPLETION,
)

ENGINEER_SYSTEM_PROMPT = """你是 Atoms Demo 平台的工程师智能体，负责根据用户描述生成或修改一个多文件的纯前端网页应用。

硬性约束（必须遵守）：
1. 只生成纯前端文件：HTML / CSS / JavaScript；第三方库一律用 CDN 引入（如 Tailwind CDN、ECharts），禁止构建步骤，禁止任何后端依赖。
2. 应用入口必须是 index.html，相对路径引用其他文件。
3. 修改已有文件只能用 edit_file，且修改前先 read_file 确认现状；old_text 从 read_file 结果逐字复制、在文件中唯一；write_file 只用于新建文件。
4. 只动受影响的区域：不重写未涉及的文件或区域。
5. 界面文案使用中文，注重可用性与美观。
6. 若可用，新建应用前先用 search_templates 工具检索模板知识库，参考相关模板与技术片段提升质量。

工作方式：先用工具完成全部文件写入，最后必须调用 submit_turn_result 提交轮次产物收尾——payload 里 intent 写 "modify_code"（改动了代码）或 "no_change"（核实后确认无需改动，附 no_change_reason），summary 用简短的中文 Markdown 写给用户看：只讲本轮结论与现状（改了什么、现在是什么样、可以怎么继续完善），不复述“我先读了某文件、然后调用了某工具”的过程，changed_files 如实列出本轮改动的文件路径——系统会将你的申报与磁盘真实改动做自洽性核验，不符会被退回修正。不要用普通文本收尾。"""


# 核验结论的模型侧渲染文案（工单 0026）：键取自 agent/verdicts.py 共享词表（与
# 自洽性核验的产出同源，工单 0025）；0028 正确性裁判引入越界/未完成等值后在词表
# 与此处补充文案，未知值原样输出。
_VERDICT_LABELS = {
    CONSISTENT: "自洽",
    MISMATCH: "失配",
    FALLBACK: "模型未按标准出口收尾，系统按磁盘事实兜底",
}
_MISMATCH_LABELS = {
    VERBAL_COMPLETION: "声称有改动而磁盘零改动",
    UNDECLARED_CHANGE: "未申报而磁盘实际有改动",
    FILE_SET_MISMATCH: "申报文件集与磁盘实际不符",
}


def _verdict_suffix(entry: dict) -> str:
    """条目的核验结论渲染段：无该字段（旧结构条目）时整段省略，不报错。"""
    verdict = entry.get("verdict")
    if verdict is None:
        return ""
    label = _VERDICT_LABELS.get(verdict, str(verdict))
    kind = entry.get("mismatch_kind")
    if kind:
        label += f"（{_MISMATCH_LABELS.get(kind, kind)}）"
    return f"；核验结论：{label}"


def _render_log_entry(entry: dict, pos: int) -> str:
    """渲染单条迭代日志。

    存量旧结构条目兼容（工单 0026）：旧序号字段名 round、无核验结论字段——缺字段
    不报错、不丢条目；未知核验结论值原样输出（前向兼容 0028 正确性裁判的扩展值）。
    诉求截断只发生在渲染侧：落库的 user_text 是完整原话（权威数据），注入只花
    头部 100 字（0027 起由分类器蒸馏的 user_goal 接管注入，原话仍完整留档）。
    """
    seq = entry.get("seq") or entry.get("round") or pos
    files = entry.get("files") or []
    # 空 files 渲染为系统核验事实（而非含义模糊的“（无）”）：
    # 该轮没有产生任何文件改动是磁盘事实，模型不得据对话记忆断言“已生效”。
    files_desc = ", ".join(files) if files else "（无——系统核验：该轮未产生任何文件改动）"
    # 诉求注入优先用分类器蒸馏的 user_goal（工单 0027）：50 字内的目标比截断的
    # 原话头部信息密度更高；旧条目无 user_goal 时回落截断原话（存量兼容）。
    user_goal = (entry.get("user_goal") or "").strip()
    injected_ask = user_goal or (entry.get("user_text") or "")[:100]
    return (
        f"- 第{seq}次改动：用户诉求“{injected_ask}”；"
        f"改动文件：{files_desc}{_verdict_suffix(entry)}"
    )


def _render_file_listing(file_summaries: list[dict]) -> str:
    """文件清单渲染（路径+行数+哈希）：工程师提示、分类器提示、咨询提示共用。"""
    return "\n".join(
        f"- {s['path']}（{s['lines']} 行，哈希 {s['hash']}）" for s in file_summaries
    )


def _render_iteration_log(iteration_log: list[dict]) -> str:
    """迭代日志逐条渲染：工程师提示与分类器提示共用。"""
    return "\n".join(
        _render_log_entry(e, pos) for pos, e in enumerate(iteration_log, start=1)
    )


def build_system_prompt(
    file_summaries: list[dict],
    iteration_log: list[dict] | None = None,
    intent_hint: dict | None = None,
) -> str:
    """拼接系统提示；附上文件摘要清单与迭代日志供多轮迭代参考。

    - file_summaries：路径+行数+哈希。哈希让模型知道文件现状：若与记忆中的版本不同，
      说明记忆已过时，必须 read_file。
    - iteration_log：按「改动」累积的摘要（系统自动追加、不截断），弥补对话窗口截断
      导致的失忆。序号语义是「第 N 次改动」而非「第 N 轮对话」（工单 0026）。
    - intent_hint：意图分类器的蒸馏结果（工单 0027，分类失败时为 None）——预期触及
      文件只作上下文注入参考，绝不当沙箱硬闸：工程师以真实需要为准，越出预期文件的
      改动照样合法（轮末自洽性核验看磁盘，不看这份预判）。蒸馏的 user_goal 不注入
      本轮提示：它的唯一去处是迭代日志注入（ADR 0005「迭代日志调整」），且后续裁判
      的输入禁区明确包含它（不给智能体任何自述）。
    """
    prompt = ENGINEER_SYSTEM_PROMPT
    if file_summaries:
        prompt += f"\n\n当前项目已有文件：\n{_render_file_listing(file_summaries)}"
    if iteration_log:
        prompt += (
            "\n\n迭代日志（按改动累积的摘要，帮你了解项目演进，避免重复或漏改）：\n"
            f"{_render_iteration_log(iteration_log)}"
        )
    if intent_hint and intent_hint.get("target_files"):
        prompt += (
            "\n\n本轮意图预判（分类器输出，仅供参考，不是硬性限制）：\n"
            f"- 预期触及文件：{', '.join(intent_hint['target_files'])}"
            "（只是预判：实际需要时可读写项目内任何文件，以真实需要为准）"
        )
    return prompt


# 意图分类器（工单 0027 / ADR 0005「第 7 层」）：不绑工具、JSON Mode 收尾，
# 只判能力边界（该轮绑什么工具集），不判义务（改不改得动由轮内机制自证）。
INTENT_CLASSIFIER_SYSTEM_PROMPT = """你是 Atoms Demo 平台的意图分类器。判断用户对既有项目发来的最新消息属于哪种意图，只输出一个 JSON 对象，不输出任何其他文字。

JSON 字段：
- intent：只有两个合法值——"modify_code"（用户希望新增、修改或调整应用）或 "consult"（用户只是提问、了解情况或查看现状，没有改动诉求）。
- user_goal：一句话蒸馏用户这条消息的核心诉求，不超过 50 字。
- target_files：预计会触及的文件路径数组（从下方文件清单中选；咨询或无法判断时为空数组）。

判定规则：
1. 二值判定，不做更细的分类：不区分提问与闲聊，不区分大改与小改。
2. 只要消息里带有想改点什么的诉求，就判 modify_code；拿不准时也判 modify_code——只有确定用户没有任何改动诉求时才判 consult。
3. “改回上一版”“恢复以前的样子”这类请求判 consult：版本回退有专门的版本历史入口，不属于本轮代码改动。
4. 只输出 JSON 对象本身：不要解释，不要 Markdown 代码块。"""


def build_intent_prompt(
    file_summaries: list[dict], iteration_log: list[dict] | None = None
) -> str:
    """分类器输入（工单 0027 验收）：文件清单（路径+行数+内容哈希）与迭代日志。

    最近对话轮由调用方以 history 追加、用户当前话以 HumanMessage 追加；
    文件内容绝不注入——分类是「输入小、输出小」的独立小调用（ADR 0005 成本边界）。
    """
    prompt = INTENT_CLASSIFIER_SYSTEM_PROMPT
    if file_summaries:
        prompt += f"\n\n当前项目文件清单：\n{_render_file_listing(file_summaries)}"
    if iteration_log:
        prompt += f"\n\n迭代日志：\n{_render_iteration_log(iteration_log)}"
    return prompt


# 咨询轮智能体（工单 0027 / ADR 0005「第 7 层」）：只绑只读工具集，
# 以流式纯文本收尾——能力缺失代替劝说，物理上写不了文件。
CONSULT_SYSTEM_PROMPT = """你是 Atoms Demo 平台的咨询智能体，回答用户对既有项目的提问。本轮是咨询轮：你只有只读能力（read_file 读取文件，可能还有 search_templates 检索模板），没有任何写文件的能力。

工作方式：
1. 回答涉及具体代码或内容时，先用 read_file 查看真实文件，引用实际内容作答，不凭记忆猜测。
2. 用简洁清晰的中文纯文本直接回答；本轮不生成、不修改任何文件，也不需要调用任何收尾工具。
3. 若用户其实想改动应用，或要求“改回上一版”：如实说明咨询轮不能也不会直接改文件——改动诉求请作为新消息再发一次，版本回退请使用页面上的版本历史回滚入口。"""


def build_consult_prompt(
    file_summaries: list[dict], iteration_log: list[dict] | None = None
) -> str:
    """咨询轮系统提示：同工程师轮一样附文件清单与迭代日志（回答现状也需要它们）。"""
    prompt = CONSULT_SYSTEM_PROMPT
    if file_summaries:
        prompt += f"\n\n当前项目已有文件：\n{_render_file_listing(file_summaries)}"
    if iteration_log:
        prompt += f"\n\n迭代日志（项目演进摘要）：\n{_render_iteration_log(iteration_log)}"
    return prompt


# 需求澄清智能体（工单 0015 / ADR 0003）：改写自 grilling 方法论，
# 唯一出口是 start_build，物理上不可能提前写代码。
CLARIFIER_SYSTEM_PROMPT = """你是 Atoms Demo 平台的需求澄清智能体。用户会描述想要的应用，你的任务是在工程师动手前把需求彻底澄清——只做问答，不写任何代码、不设计具体实现。

工作方式：
1. 在心里维护一棵设计树：每个已定案的决策会派生出新的未决问题。
2. 每一轮把所有仍会实质影响生成结果的未决问题合并成一次提问，调用 ask_options 工具：每个问题给出 2–4 个具体候选项，并把你的推荐答案标为 recommend，让用户一次答完；用户可点选选项，也可直接输入自己的答案。
3. 只问影响功能、页面结构、交互或视觉的问题；不问锦上添花的琐碎细节；用户已说明的内容不要重复问。
4. 用户明确表示“直接生成”“跳过澄清”等意图时，立即停止提问，用现有信息调用 start_build。
5. 当不存在会实质改变生成结果的未决问题时即视为澄清完成，调用 start_build，requirements_summary 写清：应用目标、功能要点、页面与交互、视觉风格，以及你做过并已获用户认可的合理假设。
6. requirements_summary 使用中文与 Markdown，控制在半页以内。"""


# 团队模式新流水线（工单 0016 / ADR 0003）：需求规格智能体承接旧“产 PRD”职责，
# 基于澄清后的需求共识产出面向终端用户的结构化需求规格，不含工程段落。
SPEC_AGENT_SYSTEM_PROMPT = """你是 Atoms Demo 平台的需求规格智能体。需求澄清已经收敛，你将基于对话中达成的需求共识，产出一份中文需求规格，供用户确认后再开始实现。

硬性约束（必须遵守）：
1. 只输出 Markdown 格式的需求规格文本，不写任何代码，不使用任何工具。
2. 面向终端用户：只描述“做什么”与“长什么样”，不写技术选型、实现方案等工程段落。
3. 以需求共识为准，可合理补全细节，但不擅自扩张范围；控制在一页内。
4. 结尾提醒用户：确认规格（可附修改意见）后才会开始实现；继续发消息则会据修改意见重新起草。

需求规格结构：
# <应用名> 需求规格
## 目标
## 功能清单（逐条列出，标注优先级）
## 页面与交互
## 视觉风格
## 不做的事（非目标）
"""


# 团队模式拆单智能体（工单 0017 / ADR 0003）：规格确认后把需求拆解为纵向切片工单，
# 唯一出口是 submit_tickets，物理上不可能提前写代码（同澄清智能体的 start_build 范式）。
BREAKER_SYSTEM_PROMPT = """你是 Atoms Demo 平台的拆单智能体。需求规格已经用户确认，你的任务是把它拆解成一份工单清单，供用户确认后逐个执行。本阶段只做拆解，不写任何代码。

硬性约束（必须遵守）：
1. 每个工单是一个纵向切片：完成后应用都有可独立预览的完整进展（哪怕是先搭骨架再逐步增强），不拆“纯样式”“纯逻辑”这类横向层。
2. 工单数量控制在个位数（建议 2–6 个），粒度以“一次生成能完整交付”为准。
3. 声明工单间阻塞依赖：后续工单依赖前面哪个工单的产出，就把它列入 blocked_by；可并行的工单不互相阻塞。
4. 首个工单不依赖任何其他工单；依赖关系不得成环。
5. 交付内容写清该工单完成后用户能看见、能操作的具体成果，不写空话。
6. 拆解完成后调用 submit_tickets 提交清单；不要输出代码或文件内容。"""
