"""智能体生成循环：工具调用循环 + 事件流。

循环不绑定具体模型实现，任何满足以下约定的对象都可驱动它：
- `bind_tools(tools) -> self`（可选）
- `astream(messages)` 产出 AIMessageChunk（可选，逐字流式文本）
- `ainvoke(messages) -> AIMessage`（astream 缺省时的回退，如可编程伪模型）

测试通过 tests/fake_model.py 的可编程伪模型经同一接口注入，
任何测试不得调用真实 MiniMax API（规格 0001 Testing Decisions）。
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

RETRY_BACKOFF_SECONDS = 0.5


@dataclass
class AgentEvent:
    """一次生成过程中的可推送事件：text | tool | done | error。"""

    type: str
    data: dict = field(default_factory=dict)


def summarize_args(args: dict) -> dict:
    """工具参数摘要：大段内容只留长度，避免把整个文件内容推给前端。"""
    summary = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 200:
            summary[k] = f"<{len(v)} 字符>"
        else:
            summary[k] = v
    return summary


async def _attempt_stream(bound, messages: list, holder: dict) -> AsyncIterator[AgentEvent]:
    """单次模型调用：逐字产出文本 delta，累积结果写入 holder["msg"]。"""
    if hasattr(bound, "astream"):
        accumulated = None
        async for chunk in bound.astream(messages):
            content = getattr(chunk, "content", "") or ""
            if content:
                yield AgentEvent("text", {"content": content})
            accumulated = chunk if accumulated is None else accumulated + chunk
        if accumulated is None:
            raise RuntimeError("模型未返回任何内容")
        holder["msg"] = accumulated
    else:
        msg = await bound.ainvoke(messages)
        if getattr(msg, "content", ""):
            yield AgentEvent("text", {"content": msg.content})
        holder["msg"] = msg


async def run_generation(
    model,
    tools: list,
    tool_executor,
    system_prompt: str,
    history: list,
    user_text: str,
    max_steps: int = 20,
    max_retries: int = 2,
) -> AsyncIterator[AgentEvent]:
    """执行一轮生成：模型 ⇄ 工具循环直至模型给出最终文本。

    history 为 langchain 消息列表（不含本轮用户输入）。
    tool_executor 签名：(tools, name, args) -> (是否成功, 结果文本)。
    """
    bound = model.bind_tools(tools) if hasattr(model, "bind_tools") else model
    messages = [SystemMessage(content=system_prompt), *history, HumanMessage(content=user_text)]

    for _ in range(max_steps):
        # 单步模型调用（含重试）：缓冲事件，成功才外发，失败尝试的半截流不泄漏
        holder: dict = {}
        pending: list[AgentEvent] = []
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            pending = []
            try:
                async for event in _attempt_stream(bound, messages, holder):
                    pending.append(event)
                last_error = None
                break
            except Exception as e:  # noqa: BLE001 — 网络/API 抖动须重试而非直接失败
                last_error = e
                if attempt < max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        if last_error is not None:
            yield AgentEvent("error", {"detail": f"模型调用失败: {last_error}"})
            return

        for event in pending:
            yield event
        msg = holder["msg"]

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            yield AgentEvent("done", {"text": getattr(msg, "content", "") or ""})
            return

        messages.append(msg)
        for call in tool_calls:
            name, args, call_id = call["name"], call.get("args") or {}, call["id"]
            yield AgentEvent("tool", {"name": name, "args": summarize_args(args), "status": "start"})
            ok, result = tool_executor(tools, name, args)
            yield AgentEvent(
                "tool",
                {
                    "name": name,
                    "args": summarize_args(args),
                    "status": "done" if ok else "error",
                    "result": result[:500],
                },
            )
            messages.append(ToolMessage(content=result, tool_call_id=call_id))

    yield AgentEvent("error", {"detail": f"智能体超过最大步数（{max_steps}）仍未完成"})
