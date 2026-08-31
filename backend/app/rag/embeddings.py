"""embedding 工厂（工单 0009 / ADR 0002）：生产用 MiniMax embo-01，测试注入桩。

与模型工厂同一模式（见 agent/model.py）：app.state.embedding_factory 可被
测试替换，任何测试不得调用真实 embedding 服务。
"""

from ..config import Settings


class MiniMaxEmbedder:
    """满足最小约定 `embed(text) -> list[float]` 的 MiniMax embedding 封装。"""

    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise RuntimeError("Embedding 未配置：请设置环境变量 ATOMS_LLM_API_KEY")
        from langchain_community.embeddings import MiniMaxEmbeddings

        self._impl = MiniMaxEmbeddings(
            model=settings.embedding_model, minimax_api_key=settings.llm_api_key
        )

    def embed(self, text: str) -> list[float]:
        return self._impl.embed_query(text)


def default_embedding_factory(settings: Settings) -> MiniMaxEmbedder:
    return MiniMaxEmbedder(settings)
