"""智能体生成循环：工具调用循环 + 事件流。

循环不绑定具体模型实现，任何满足以下约定的对象都可驱动它：
- `bind_tools(tools) -> self`（可选）
- `astream(messages)` 产出 AIMessageChunk（可选，逐字流式文本）
- `ainvoke(messages) -> AIMessage`（astream 缺省时的回退，如可编程伪模型）

测试通过 tests/fake_model.py 的可编程伪模型经同一接口注入，
任何测试不得调用真实 MiniMax API（规格 0001 Testing Decisions）。
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

RETRY_BACKOFF_SECONDS = 0.5

# MiniMax 等推理模型把思考过程以 思考标签块 行内混在正文 content 里输出，
# 循环负责把它拆成独立的 thinking 事件，前端才能以区分样式展示。
# 标签用拼接构造而非字面量，避免源码被外部工具误改写。
THINK_OPEN = "<" + "think" + ">"
THINK_CLOSE = "</" + "think" + ">"


@dataclass
class AgentEvent:
    """一次生成过程中的可推送事件：text | thinking | tool | done | error。"""

    type: str
    data: dict = field(default_factory=dict)


class ThinkSplitter:
    """把增量输出拆为 (thinking | text) 两路片段。

    标签可能被切断在相邻 chunk 上（如开标签只剩前半截），
    因此尾部确实是标签前缀的部分暂不发射，留在缓冲区等下一个片段拼接。
    """

    def __init__(self):
        self._in_think = False
        self._buffer = ""

    def feed(self, piece: str) -> list[tuple[str, str]]:
        self._buffer += piece
        return self._drain(final=False)

    def flush(self) -> list[tuple[str, str]]:
        """流结束时排干缓冲区（未闭合的思考段并入 thinking，残留前缀按正文照发）。"""
        return self._drain(final=True)

    @staticmethod
    def _hold_len(buf: str, tag: str) -> int:
        """尾部需要保留等待拼接的长度：最长的"是标签前缀的后缀"，否则为 0。

        只扣真正可能是标签开头的尾部，避免短正文被整段扣住
        （否则前端长时间看不到增量，且失败重试会误判为尚未外发）。
        """
        for n in range(min(len(buf), len(tag) - 1), 0, -1):
            if tag.startswith(buf[-n:]):
                return n
        return 0

    def _drain(self, final: bool) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        while True:
            kind = "thinking" if self._in_think else "text"
            tag = THINK_CLOSE if self._in_think else THINK_OPEN
            idx = self._buffer.find(tag)
            if idx != -1:
                if idx > 0:
                    out.append((kind, self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(tag):]
                self._in_think = not self._in_think
                continue
            # 未找到完整标签：发射确定安全的部分；流结束（final）时不保留尾部
            hold = 0 if final else self._hold_len(self._buffer, tag)
            safe_len = len(self._buffer) - hold
            if safe_len > 0:
                out.append((kind, self._buffer[:safe_len]))
                self._buffer = self._buffer[safe_len:]
            break
        return [(k, s) for k, s in out if s]


def strip_think_blocks(text: str) -> str:
    """去除完整思考块与尾部未闭合的思考段（最终结论的兜底清洗）。"""
    pattern = re.escape(THINK_OPEN) + ".*?" + re.escape(THINK_CLOSE)
    text = re.sub(pattern, "", text, flags=re.DOTALL)
    text = re.sub(re.escape(THINK_OPEN) + ".*$", "", text, flags=re.DOTALL)
    return text.strip()


def summarize_args(args: dict) -> dict:
    """工具参数摘要：大段内容只留长度，避免把整个文件内容推给前端。"""
    summary = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 200:
            summary[k] = f"<{len(v)} 字符>"
        else:
            summary[k] = v
    return summary


# 完成断言的中文措辞检测（固定短语表 + 正则族 + 三级升级链）已于工单 0025 整体退役：
# 对措辞的猜测被自洽性核验取代——比对声明改动与磁盘真实改动，纯集合运算，不看措辞。
# 循环只保留一个通用的收尾钩子 final_text_hook，由路由层注入核验与降级链逻辑。


async def _attempt_stream(bound, messages: list, holder: dict) -> AsyncIterator[AgentEvent]:
    """单次模型调用：逐字产出 text/thinking delta，累积结果写入 holder["msg"]。"""
    if hasattr(bound, "astream"):
        splitter = ThinkSplitter()
        accumulated = None
        async for chunk in bound.astream(messages):
            # Qwen3 等经 extra_body 开启思考的模型，思考文本走独立的 reasoning_content
            # 增量（由 ThinkingChatOpenAI 补进 additional_kwargs），已是纯思考、无需再拆标签
            reasoning = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
            if reasoning:
                yield AgentEvent("thinking", {"content": reasoning})
            content = getattr(chunk, "content", "") or ""
            if content:
                for kind, piece in splitter.feed(content):
                    yield AgentEvent(kind, {"content": piece})
            accumulated = chunk if accumulated is None else accumulated + chunk
        for kind, piece in splitter.flush():
            yield AgentEvent(kind, {"content": piece})
        if accumulated is None:
            raise RuntimeError("模型未返回任何内容")
        holder["msg"] = accumulated
    else:
        msg = await bound.ainvoke(messages)
        # 非流式回退也过拆分器，保证前端两路事件的消费路径一致
        splitter = ThinkSplitter()
        parts = splitter.feed(getattr(msg, "content", "") or "") + splitter.flush()
        for kind, piece in parts:
            yield AgentEvent(kind, {"content": piece})
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
    final_text_hook: Callable[[str], str | None] | None = None,
) -> AsyncIterator[AgentEvent]:
    """执行一轮生成：模型 ⇄ 工具循环直至模型给出最终文本。

    history 为 langchain 消息列表（不含本轮用户输入）。
    tool_executor 签名：(tools, name, args) -> (是否成功, 结果文本)。
    final_text_hook：收尾钩子（工单 0025 / ADR 0005 降级链的路由层注入点）。
      模型未调任何工具、试图以普通文本收尾时在发出 done 之前调用，入参是
      洗去思考块的最终文本；返回一段文本则作为回喂消息注入并继续循环
      （回喂决策与次数上限由钩子自己掌握），返回 None 则照常以 done 收尾。
      钩子也可以抛出路由层的控制流信号（如轮次产物终结出口的
      _SubmitTurnResultInvoked），异常沿生成器向上传播、由调用方捕获。
    """
    bound = model.bind_tools(tools) if hasattr(model, "bind_tools") else model
    messages = [SystemMessage(content=system_prompt), *history, HumanMessage(content=user_text)]

    for _ in range(max_steps):
        # 单步模型调用（含重试）：事件实时外发，前端才有打字机效果；
        # 尚未外发任何内容时失败可静默重试，一旦已外发则不重试（避免半截流重复）直接以 error 收尾
        holder: dict = {}
        emitted = False
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async for event in _attempt_stream(bound, messages, holder):
                    emitted = True
                    yield event
                last_error = None
                break
            except Exception as e:  # noqa: BLE001 — 网络/API 抖动须重试而非直接失败
                last_error = e
                if emitted or attempt >= max_retries:
                    break
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        if last_error is not None:
            yield AgentEvent("error", {"detail": f"模型调用失败: {last_error}"})
            return

        msg = holder["msg"]

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            # 最终文本洗去思考块：流式增量里已拆走，这里是兜底（如非流式回退）
            final_text = strip_think_blocks(getattr(msg, "content", "") or "")
            if final_text_hook is not None:
                feedback = final_text_hook(final_text)
                if feedback is not None:
                    messages.append(msg)
                    messages.append(HumanMessage(content=feedback))
                    continue
            yield AgentEvent("done", {"text": final_text})
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
