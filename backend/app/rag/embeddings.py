"""embedding 工厂（工单 0009 / ADR 0002）：生产默认走 OpenAI 兼容端点，测试注入桩。

与模型工厂同一模式（见 agent/model.py）：app.state.embedding_factory 可被
测试替换，任何测试不得调用真实 embedding 服务。

传输走 OpenAI 兼容端点：默认复用 LLM 的 base_url（MiniMax OpenAI 兼容
接口同样提供 embo-01）；设 ATOMS_EMBEDDING_BASE_URL 可指向任意 OpenAI
兼容 embedding 服务（ATOMS_EMBEDDING_API_KEY 缺省时复用 LLM Key）。
工单 0012 回归发现：langchain_community.MiniMaxEmbeddings 强依赖
MINIMAX_GROUP_ID，在生产环境直接构建即失败，故弃用改走兼容端点。
"""

from ..config import Settings


class OpenAICompatibleEmbedder:
    """满足最小约定 `embed(text) -> list[float]` 的 OpenAI 兼容 embedding 封装。"""

    def __init__(self, settings: Settings):
        api_key = settings.embedding_api_key or settings.llm_api_key
        if not api_key:
            raise RuntimeError(
                "Embedding 未配置：请设置环境变量 ATOMS_LLM_API_KEY 或 ATOMS_EMBEDDING_API_KEY"
            )
        from langchain_openai import OpenAIEmbeddings

        # LiteLLM 风格的 "provider:model" 前缀不被兼容端点接受，取冒号后的真实模型名

        self._impl = OpenAIEmbeddings(
            model=settings.embedding_model.split(":", 1)[-1],
            base_url=settings.embedding_base_url or settings.llm_base_url,
            api_key=api_key,
            # 非 OpenAI 官方端点不需要按 token 切分，直接送原文
            check_embedding_ctx_length=False,
        )

    def embed(self, text: str) -> list[float]:
        return self._impl.embed_query(text)


def default_embedding_factory(settings: Settings) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(settings)
