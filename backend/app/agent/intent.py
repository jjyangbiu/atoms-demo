"""意图分类（工单 0027 / ADR 0005「第 7 层」）：迭代轮分派前的独立小调用。

意图只有二值：modify_code（该轮绑完整工具集）/ consult（该轮只绑只读工具集）。
分类器只决定能力边界、不决定义务——假阳性（问题被判成改动）有轮内自愈：
终结出口可自报 no_change；假阴性（改动诉求被判成咨询）没有自愈机制（只读轮
物理上改不了文件），因此分类故障一律静默降级为 modify_code，绝不阻断用户轮次。

通道选择：分类不绑工具，以 JSON Mode（response_format）收尾——response_format
与工具是互斥的输出通道（房规同 tools.py 的终结出口注释）。JSON Mode 只保证
语法合法，服务端 schema 校验必须做（parse_intent_payload）；非法输出回喂错误
文案让模型修正一次，仍失败则返回 None 由调用方静默降级。
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from .loop import strip_think_blocks

INTENT_MODIFY_CODE = "modify_code"
INTENT_CONSULT = "consult"
INTENT_ENUM = (INTENT_MODIFY_CODE, INTENT_CONSULT)
# user_goal 蒸馏上限（工单 0027 验收）：超长在解析侧截断，不算校验失败
USER_GOAL_MAX_CHARS = 50

# 版本回退语（分类器 prompt 规则 3）：这类请求即便含「改」字也应留在 consult——
# 版本回退有专门的历史入口，不属于本轮代码改动。负向标记优先于改动动词判定。
_ROLLBACK_MARKERS = (
    "改回",
    "回上一版",
    "回上个版",
    "回到上一",
    "恢复以前",
    "恢复之前",
    "恢复到",
    "回滚",
    "还原",
    "退回",
)
# 改动动词（子串命中即视为带改动诉求）：只在分类器判 consult 时作二次防线用。
# 房规（intent 模块头注）：假阳性（咨询被救成改动）有轮内自愈——终结出口可自报
# no_change；假阴性（改动被判成咨询）落进只读轮物理上改不了、且无回退机制。故此处
# 刻意偏向改动，宁可多绑工具，词表从宽。
_MODIFY_MARKERS = (
    "改",
    "换",
    "加",
    "增",
    "删",
    "去掉",
    "调整",
    "优化",
    "修复",
    "替换",
    "更新",
    "变成",
    "设成",
    "设为",
    "实现",
    "支持",
    "做一个",
    "做个",
)


def looks_like_modify_request(user_text: str) -> bool:
    """确定性关键词判定：这条用户话是否明显带改动诉求（分类器判 consult 时的二次防线）。

    先排除版本回退语（含「改」字也保持 consult，尊重分类器 prompt 规则 3），再看是否
    命中改动动词。纯确定性、零成本，只在分类器意外把明确改动诉求判成 consult 时兜底，
    绝不覆盖真正的提问/回退轮。
    """
    text = user_text or ""
    if any(m in text for m in _ROLLBACK_MARKERS):
        return False
    return any(m in text for m in _MODIFY_MARKERS)


def parse_intent_payload(raw: str) -> tuple[dict | None, str]:
    """校验分类输出 JSON；非法时返回 (None, 错误文案) 交还模型修正（房规同其他 parse_*）。

    约束：JSON 对象；intent 限于二值枚举；user_goal 非空（超长截断到 50 字）；
    target_files 可缺省（视为空数组），给出则须为非空路径字符串数组——它只用于
    上下文注入与轮末对照参考，绝不当沙箱硬闸（工单 0027 验收）。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, "输出不是合法 JSON。"
    if not isinstance(data, dict):
        return None, "输出必须是 JSON 对象。"
    intent = str(data.get("intent") or "").strip()
    if intent not in INTENT_ENUM:
        return None, f"intent 必须是 {' 或 '.join(INTENT_ENUM)} 之一。"
    user_goal = str(data.get("user_goal") or "").strip()
    if not user_goal:
        return None, "user_goal 不能为空：须用一句话蒸馏用户核心诉求。"
    files_raw = data.get("target_files")
    if files_raw is None:
        files_raw = []
    if not isinstance(files_raw, list):
        return None, "target_files 必须是文件路径数组（无预期文件时为空数组）。"
    target_files = [str(f or "").strip() for f in files_raw]
    if any(not f for f in target_files):
        return None, "target_files 含空路径。"
    return (
        {
            "intent": intent,
            "user_goal": user_goal[:USER_GOAL_MAX_CHARS],
            "target_files": target_files,
        },
        "",
    )


async def classify_intent(
    model, system_prompt: str, history: list, user_text: str
) -> dict | None:
    """执行一次意图分类；任何失败返回 None，调用方静默降级为 modify_code。

    输入契约（工单 0027 验收）：文件清单（路径+行数+哈希，经 system_prompt）、
    迭代日志（经 system_prompt）、最近对话轮（history）、用户当前话（user_text）；
    不注入文件内容——分类是「输入小、输出小」的独立小调用（ADR 0005 成本边界）。

    校验失败或枚举越界 → 回喂错误文案让模型修正一次（房规同澄清/拆单/终结出口）；
    仍失败返回 None——调用方静默降级为 modify_code，绝不阻断用户轮次。
    """
    # JSON Mode：真实 ChatOpenAI 经 bind 强制 json_object；伪模型无 bind 则原样调用
    caller = (
        model.bind(response_format={"type": "json_object"})
        if hasattr(model, "bind")
        else model
    )
    messages = [
        SystemMessage(content=system_prompt),
        *history,
        HumanMessage(content=user_text),
    ]
    for _ in range(2):
        try:
            msg = await caller.ainvoke(messages)
        except Exception:  # noqa: BLE001 — 分类故障绝不阻断用户轮次
            return None
        # 推理模型会把思考混进 content：解析前先洗掉行内思考块（房规同循环收尾）
        parsed, reason = parse_intent_payload(
            strip_think_blocks(getattr(msg, "content", "") or "")
        )
        if parsed is not None:
            return parsed
        messages = [
            *messages,
            msg,
            HumanMessage(
                content=(
                    f"分类输出不合法：{reason} "
                    "请只输出一个符合约定 schema 的 JSON 对象（intent / user_goal / "
                    "target_files），不要输出任何其他文字。"
                )
            ),
        ]
    return None
