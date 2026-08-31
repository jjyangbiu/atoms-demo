"""模板知识库 RAG + 画廊语义搜索端到端测试（工单 0009）。

验收要点：
- 种子模板随启动幂等灌入（重复启动不产生重复条目）
- 生成流程出现"正在检索模板"工具事件，且检索结果实际进入智能体上下文
- 画廊语义搜索按意图命中相关应用（非关键词精确匹配）
- 已发布应用自动沉淀进知识库，下架/删除后移除
- 测试使用临时目录的 Milvus Lite 实例，以桩替代真实 embedding 服务
"""

from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage

from app.main import create_app
from app.rag.seeds import SEED_TEMPLATES
from app.rag.store import KnowledgeStore, get_knowledge_store
from conftest import use_fake_embeddings, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project
from test_world import _generate_and_publish


class _BrokenStore:
    """检索运行期必抛错的伪知识库：验证降级路径。"""

    def search(self, query, top_k=5, source=None):
        raise RuntimeError("embedding 服务宕机")


class TestSeedTemplates:
    def test_seed_idempotent_across_restarts(self, settings, app):
        """两个应用实例共享同一向量库文件（模拟重启）：条目数不变。"""
        store = get_knowledge_store(app)
        assert store.entry_count() == len(SEED_TEMPLATES)
        assert store.entry_count("seed") == len(SEED_TEMPLATES)

        restarted = create_app(settings)
        use_fake_embeddings(restarted)
        store2 = get_knowledge_store(restarted)
        assert store2.entry_count("seed") == len(SEED_TEMPLATES)

    def test_reopen_with_released_collection_still_readable(self, settings, app):
        """重启后集合加载状态不保留（released）：重开 store 后种子灌入与检索仍可用。"""
        store = get_knowledge_store(app)
        store.client.release_collection("knowledge")  # 等价于进程重启后的落库状态
        reopened = KnowledgeStore(settings.milvus_uri, app.state.embedding_factory(settings))
        reopened.seed(SEED_TEMPLATES)
        assert reopened.entry_count("seed") == len(SEED_TEMPLATES)
        assert [h["title"] for h in reopened.search("记账", top_k=3)]

    def test_milvus_lives_in_temp_dir_and_uses_stub_embeddings(self, settings, app):
        """向量库落在临时目录；灌入只走桩 embedding，不触碰真实服务。"""
        embedder = app.state.embedding_factory(settings)
        get_knowledge_store(app)
        assert Path(settings.milvus_uri).exists()
        assert embedder.calls >= len(SEED_TEMPLATES)


class TestSearchTemplatesTool:
    def test_tool_event_streams_and_result_enters_context(self, app, client, auth_headers):
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("search_templates", {"query": "记账"})]},
                {
                    "tool_calls": [
                        ("write_file", {"path": "index.html", "content": "<h1>记账</h1>"})
                    ]
                },
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个记账工具")

        # SSE 出现"正在检索模板"工具事件（start 与 done 成对）
        search_events = [e for e in events if e["type"] == "tool" and e["name"] == "search_templates"]
        assert [e["status"] for e in search_events] == ["start", "done"]
        assert search_events[1]["status"] == "done" and "【" in search_events[1]["result"]

        # 检索结果实际进入智能体上下文（后续调用的消息里有检索结果的 ToolMessage）
        last_call = model.received_messages[-1]
        tool_msgs = [m for m in last_call if isinstance(m, ToolMessage)]
        assert any("记账" in m.content and "【" in m.content for m in tool_msgs)

    def test_generation_without_store_still_works(self, app, settings, client, auth_headers):
        """embedding 环境缺失时降级：无检索工具，生成主链路不受影响。"""

        def broken_factory(s):
            raise RuntimeError("embedding 不可用")

        app.state.embedding_factory = broken_factory
        app.state.knowledge_store = None
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>hi</h1>"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做个页面")
        assert events[-1]["type"] == "done"
        assert not any(
            e["type"] == "tool" and e["name"] == "search_templates" for e in events
        )


class TestWorldSemanticSearch:
    def test_semantic_search_hits_by_intent(self, app, client, auth_headers):
        """"家庭开支记录本"没有任何关键词重合也能命中记账应用，不命中天气应用。"""
        _generate_and_publish(
            app, client, auth_headers, name="小账本", prompt="做一个记账工具"
        )
        _generate_and_publish(
            app, client, auth_headers, name="天气看板", prompt="做一个天气查询看板"
        )

        apps = TestClient(app).get("/api/world", params={"q": "家庭开支记录本"}).json()
        assert [a["title"] for a in apps] == ["小账本"]

    def test_semantic_search_excludes_unpublished(self, app, client, auth_headers):
        """未发布项目不进知识库，语义搜索也搜不到。"""
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>私</h1>"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个私密记账本")

        apps = TestClient(app).get("/api/world", params={"q": "记账"}).json()
        assert apps == []

    def test_search_falls_back_to_keyword_without_store(self, app, client, auth_headers):
        """知识库不可用时降级为标题/描述关键词包含匹配。"""
        _generate_and_publish(app, client, auth_headers, name="小账本", prompt="做一个记账工具")

        def broken_factory(s):
            raise RuntimeError("embedding 不可用")

        app.state.embedding_factory = broken_factory
        app.state.knowledge_store = None

        apps = TestClient(app).get("/api/world", params={"q": "账本"}).json()
        assert [a["title"] for a in apps] == ["小账本"]
        assert TestClient(app).get("/api/world", params={"q": "天差地别"}).json() == []

    def test_search_runtime_failure_falls_back(self, app, client, auth_headers):
        """知识库已构建但检索运行期报错（如 embedding 服务宕机）：同样降级不 500。"""
        _generate_and_publish(app, client, auth_headers, name="小账本", prompt="做一个记账工具")
        get_knowledge_store(app)  # 先正常构建缓存，再让检索抛错
        app.state.knowledge_store = _BrokenStore()

        resp = TestClient(app).get("/api/world", params={"q": "账本"})
        assert resp.status_code == 200
        assert [a["title"] for a in resp.json()] == ["小账本"]


class TestPublishSinkLoop:
    def test_published_app_sinks_and_benefits_generation(self, app, client, auth_headers):
        """发布后进知识库：生成前的检索能取到它，形成沉淀闭环。"""
        project, pub = _generate_and_publish(
            app, client, auth_headers, name="小账本", prompt="做一个记账工具"
        )
        store = get_knowledge_store(app)
        hits = store.search("记账", top_k=3, source="published")
        assert [h["slug"] for h in hits] == [pub["slug"]]

        # 之后的生成检索受益：search_templates 结果包含已发布应用的标题
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("search_templates", {"query": "记账"})]},
                {"text": "完成。"},
            ],
        )
        _stream_messages(client, auth_headers, project["id"], "参考已有应用再聊聊")
        tool_msgs = [
            m for m in model.received_messages[-1] if isinstance(m, ToolMessage)
        ]
        assert any("小账本" in m.content for m in tool_msgs)

    def test_unpublish_removes_from_knowledge(self, app, client, auth_headers):
        project, pub = _generate_and_publish(
            app, client, auth_headers, name="小账本", prompt="做一个记账工具"
        )
        assert client.delete(
            f"/api/projects/{project['id']}/publish", headers=auth_headers
        ).status_code == 204

        store = get_knowledge_store(app)
        assert store.search("记账", top_k=3, source="published") == []
        assert TestClient(app).get("/api/world", params={"q": "记账"}).json() == []

    def test_delete_project_removes_sunk_entry(self, app, client, auth_headers):
        project, pub = _generate_and_publish(
            app, client, auth_headers, name="小账本", prompt="做一个记账工具"
        )
        assert client.delete(
            f"/api/projects/{project['id']}", headers=auth_headers
        ).status_code == 204

        store = get_knowledge_store(app)
        assert store.search("记账", top_k=3, source="published") == []
