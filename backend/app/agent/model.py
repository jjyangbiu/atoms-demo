"""模型工厂：生产环境构建 OpenAI 兼容客户端（MiniMax / Qwen3 等）；测试注入伪模型。

辅助模型工厂（工单 0027 / ADR 0005「成本边界」）：意图分类与后续正确性裁判
共用的独立小调用模型，取 llm_utility_model（置空回落 llm_model）。

思考过程回流：Qwen3 等经 OpenAI 兼容端点的推理模型把思考文本放在流式增量的
`reasoning_content` 字段里，而 langchain-openai 基类明确不提取该字段（见其源码
文档："reasoning_content ... are not extracted. Use a provider-specific subclass"）。
故此处用 ThinkingChatOpenAI 子类在 chunk 转换时把 reasoning_content 补回
additional_kwargs，交由循环层拆成 thinking 事件推送前端（与 MiniMax 行内 <think>
同一条展示/持久化通路）。
"""

from ..config import Settings


def _build_chat_model(settings: Settings, model_name: str, *, enable_thinking: bool = False):
    if not settings.llm_api_key:
        raise RuntimeError("LLM 未配置：请设置环境变量 ATOMS_LLM_API_KEY")
    from langchain_openai import ChatOpenAI

    class ThinkingChatOpenAI(ChatOpenAI):
        """在流式 chunk 转换时补回 reasoning_content（基类会丢弃该字段）。"""

        def _convert_chunk_to_generation_chunk(
            self, chunk, default_chunk_class, base_generation_info
        ):
            generation_chunk = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info
            )
            if generation_chunk is None:
                return None
            try:
                choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices", [])
                if choices:
                    delta = choices[0].get("delta") or {}
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
            except Exception:  # noqa: BLE001 — 补采思考纯属增益，绝不能影响正文流
                pass
            return generation_chunk

    kwargs = dict(
        model=model_name,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
    )
    if enable_thinking:
        # enable_thinking 非 OpenAI 标准参数，须经 extra_body 透传；开启后模型以
        # reasoning_content 增量回流思考文本，由 ThinkingChatOpenAI 补采。
        kwargs["extra_body"] = {"enable_thinking": True}
        return ThinkingChatOpenAI(**kwargs)
    return ChatOpenAI(**kwargs)


def default_model_factory(settings: Settings):
    # 主生成模型（engineer）：按配置开启思考过程输出。
    return _build_chat_model(
        settings, settings.llm_model, enable_thinking=settings.llm_enable_thinking
    )


def default_utility_model_factory(settings: Settings):
    # 辅助模型（意图分类 / 正确性裁判）走 JSON Mode、输出小且需快，恒不开思考。
    return _build_chat_model(settings, settings.llm_utility_model or settings.llm_model)
