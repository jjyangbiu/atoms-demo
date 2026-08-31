"""模板知识库 RAG（工单 0009）：embedding、种子模板与 Milvus Lite 存取。"""

from .seeds import SEED_TEMPLATES
from .store import KnowledgeStore, get_knowledge_store, maybe_knowledge_store

__all__ = [
    "SEED_TEMPLATES",
    "KnowledgeStore",
    "get_knowledge_store",
    "maybe_knowledge_store",
]
