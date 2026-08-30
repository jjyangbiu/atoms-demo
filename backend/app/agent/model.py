"""模型工厂：生产环境构建 MiniMax（OpenAI 兼容）客户端；测试注入伪模型。"""

from ..config import Settings


def default_model_factory(settings: Settings):
    if not settings.llm_api_key:
        raise RuntimeError("LLM 未配置：请设置环境变量 ATOMS_LLM_API_KEY")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
    )
