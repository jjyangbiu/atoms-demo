"""可编程伪模型：按预排脚本驱动智能体循环，替代真实 MiniMax。

脚本是一个步骤列表，每步为以下之一：
- {"text": "..."}                 → 返回纯文本（循环结束）
- {"tool_calls": [(name, args)]}  → 返回工具调用（循环继续）
- {"tool_calls": [...], "text": "..."} → 工具调用同时带文本
- Exception 实例或异常类          → 该步抛出（验证重试/错误收尾）

伪模型满足 loop.run_generation 的最小约定：bind_tools / ainvoke，
因此与真实 ChatOpenAI 走完全相同的循环代码路径。
"""

from langchain_core.messages import AIMessage


class FakeModel:
    def __init__(self, script: list):
        self.script = list(script)
        self.received_messages: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.received_messages.append(list(messages))
        if not self.script:
            return AIMessage(content="（脚本已耗尽）")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("伪模型脚本安排的异常")
        tool_calls = [
            {"name": name, "args": args, "id": f"call_{i}", "type": "tool_call"}
            for i, (name, args) in enumerate(step.get("tool_calls", []))
        ]
        return AIMessage(content=step.get("text", ""), tool_calls=tool_calls)
