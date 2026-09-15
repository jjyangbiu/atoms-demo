"""模型工厂：生产环境构建 MiniMax（OpenAI 兼容）客户端；测试注入伪模型。

辅助模型工厂（工单 0027 / ADR 0005「成本边界」）：意图分类与后续正确性裁判
共用的独立小调用模型，取 llm_utility_model（置空回落 llm_model）。
"""

from ..config import Settings


def _build_chat_model(settings: Settings, model_name: str):
    if not settings.llm_api_key:
        raise RuntimeError("LLM 未配置：请设置环境变量 ATOMS_LLM_API_KEY")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model_name,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
    )


def default_model_factory(settings: Settings):
    return _build_chat_model(settings, settings.llm_model)


def default_utility_model_factory(settings: Settings):
    return _build_chat_model(settings, settings.llm_utility_model or settings.llm_model)
