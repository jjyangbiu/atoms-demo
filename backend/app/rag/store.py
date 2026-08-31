"""Milvus Lite 知识库（工单 0009 / ADR 0002）：单集合承载种子模板与已发布应用。

- 条目主键即身份：`seed:{key}` 或 `pub:{slug}`，因此重复灌入天然幂等
- 检索结果按相似度排序；`source` 字段区分种子模板与已发布应用，
  智能体检索用全部条目，画廊语义搜索只查已发布条目
- 换 embedding 模型会导致向量空间不兼容，须删除 milvus_uri 全量重建（ADR 0002）
"""

import threading
from pathlib import Path

from ..config import Settings
from .seeds import SEED_TEMPLATES

_COLLECTION = "knowledge"
_OUTPUT_FIELDS = ["source", "slug", "title", "text"]
# 懒构建互斥：同步路由跑在线程池，冷启动后并发首访不得重复构建/重复灌入
_build_lock = threading.Lock()


class KnowledgeStore:
    """满足最小约定的向量存取：embedder 只需 `embed(text) -> list[float]`。"""

    def __init__(self, uri: str, embedder):
        from pymilvus import MilvusClient

        # Milvus Lite 以本地文件（目录形式）承载；父目录需先存在
        if "://" not in uri:
            Path(uri).parent.mkdir(parents=True, exist_ok=True)
        self.uri = uri
        self.embedder = embedder
        self.client = MilvusClient(uri=uri)
        # 加载状态不落盘：进程重启后既有集合处于 released，须显式 load；
        # 新建集合由 create_collection 自动加载，幂等调用对已加载集合无副作用
        if self.client.has_collection(_COLLECTION):
            self.client.load_collection(_COLLECTION)

    # --- 内部：集合按需创建（维度由 embedder 首次产出决定） ---

    def _ensure_collection(self, dim: int) -> None:
        from pymilvus import DataType

        if self.client.has_collection(_COLLECTION):
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=96)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("source", DataType.VARCHAR, max_length=16)
        schema.add_field("slug", DataType.VARCHAR, max_length=32)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        index_params = self.client.prepare_index_params()
        # 数据量小（百级），FLAT 精确检索即可
        index_params.add_index("vector", index_type="FLAT", metric_type="COSINE")
        self.client.create_collection(_COLLECTION, schema=schema, index_params=index_params)

    def _row(self, entry_id: str, source: str, slug: str, title: str, text: str) -> dict:
        return {
            "id": entry_id,
            "vector": self.embedder.embed(f"{title}。{text}"),
            "source": source,
            "slug": slug,
            "title": title,
            "text": text,
        }

    # --- 种子灌入：按主键幂等，重复启动不产生重复条目 ---

    def seed(self, entries: list[dict]) -> None:
        if not self.client.has_collection(_COLLECTION):
            rows = [self._row(f"seed:{e['key']}", "seed", "", e["title"], e["text"]) for e in entries]
            self._ensure_collection(len(rows[0]["vector"]))
            self.client.insert(_COLLECTION, rows)
            return
        wanted = {f"seed:{e['key']}": e for e in entries}
        ids = ", ".join(f'"{i}"' for i in wanted)
        existing = {
            row["id"]
            for row in self.client.query(_COLLECTION, filter=f"id in [{ids}]", output_fields=["id"])
        }
        missing = [
            self._row(f"seed:{e['key']}", "seed", "", e["title"], e["text"])
            for e in entries
            if f"seed:{e['key']}" not in existing
        ]
        if missing:
            self.client.upsert(_COLLECTION, missing)

    # --- 已发布应用沉淀（发布时 upsert、下架/删除时移除） ---

    def upsert_published(self, slug: str, title: str, description: str) -> None:
        row = self._row(f"pub:{slug}", "published", slug, title, description or title)
        self._ensure_collection(len(row["vector"]))
        self.client.upsert(_COLLECTION, [row])

    def remove_published(self, slug: str) -> None:
        if self.client.has_collection(_COLLECTION):
            self.client.delete(_COLLECTION, ids=[f"pub:{slug}"])

    # --- 检索：返回 [{score, source, slug, title, text}, ...] 按相似度降序 ---

    def search(self, query: str, top_k: int = 5, source: str | None = None) -> list[dict]:
        vector = self.embedder.embed(query)
        self._ensure_collection(len(vector))
        results = self.client.search(
            _COLLECTION,
            data=[vector],
            filter=f'source == "{source}"' if source else "",
            limit=top_k,
            output_fields=_OUTPUT_FIELDS,
        )
        return [{"score": hit["distance"], **hit["entity"]} for hit in results[0]]

    def entry_count(self, source: str | None = None) -> int:
        """条目计数（测试与运维观测用）。"""
        if not self.client.has_collection(_COLLECTION):
            return 0
        rows = self.client.query(
            _COLLECTION,
            filter=f'source == "{source}"' if source else "",
            output_fields=["count(*)"],
        )
        return int(rows[0]["count(*)"])


def get_knowledge_store(app) -> KnowledgeStore:
    """懒构建并灌入种子模板（按主键幂等，重启不重复）；缓存于 app.state。"""
    store = getattr(app.state, "knowledge_store", None)
    if store is None:
        with _build_lock:
            store = getattr(app.state, "knowledge_store", None)
            if store is None:
                settings: Settings = app.state.settings
                store = KnowledgeStore(settings.milvus_uri, app.state.embedding_factory(settings))
                store.seed(SEED_TEMPLATES)
                app.state.knowledge_store = store
    return store


def maybe_knowledge_store(app) -> KnowledgeStore | None:
    """环境不具备（未配置 API Key 等）时返回 None，调用方须降级而非报错。"""
    try:
        return get_knowledge_store(app)
    except Exception:  # noqa: BLE001 — 知识库是增强能力，不可用时整体功能仍须可用
        return None
