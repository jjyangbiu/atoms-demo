"""测试公共骨架：临时 SQLite + 临时存储目录 + 临时向量库 + TestClient。

这是后续所有测试的范本（见工单 0002 与规格 Testing Decisions）：
- 只测外部可观察行为（HTTP 响应），不测内部实现
- 任何测试不得依赖外部服务（真实 LLM / 真实 embedding 服务）：
  默认注入桩 embedding（工单 0009），向量库用临时目录的 Milvus Lite
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import ProjectFile
from fake_embeddings import FakeEmbedder
from fake_model import FakeModel


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_root=str(tmp_path / "storage"),
        milvus_uri=str(tmp_path / "milvus" / "atoms.db"),
        jwt_secret="test-secret-key-for-jwt-0123456789abcdef",
        cors_origins="http://localhost:5173",
        _env_file=None,
    )


@pytest.fixture
def app(settings):
    app = create_app(settings)
    # 默认装上桩 embedding：任何测试不得调用真实 embedding 服务（工单 0009）
    use_fake_embeddings(app)
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def registered_user(client) -> dict:
    """注册一个用户并返回凭证 {username, password}。"""
    creds = {"username": "alice", "password": "secret123"}
    resp = client.post("/api/auth/register", json=creds)
    assert resp.status_code == 201, resp.text
    return creds


def login(client: TestClient, username: str, password: str) -> str:
    """登录并返回 access_token（失败直接断言失败）。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
def auth_headers(client, registered_user) -> dict:
    token = login(client, registered_user["username"], registered_user["password"])
    return {"Authorization": f"Bearer {token}"}


def use_fake_model(app, script: list) -> FakeModel:
    """把应用的可编程伪模型工厂装上，返回伪模型实例供断言。"""
    model = FakeModel(script)
    app.state.model_factory = lambda settings: model
    return model


def use_fake_embeddings(app, dim: int = 256) -> FakeEmbedder:
    """把应用的桩 embedding 工厂装上，返回桩实例供断言（如调用计数）。"""
    embedder = FakeEmbedder(dim)
    app.state.embedding_factory = lambda settings: embedder
    app.state.knowledge_store = None  # 丢弃可能已缓存的旧实例，用新桩重建
    return embedder


# --- 首建流水线辅助（工单 0015） ---
#
# 工程师模式新建项目的首条消息先走需求澄清：把它预排在伪模型脚本首位，
# 即可让旧测试以最小改动穿过“澄清 → 共识确认 → 生成”链路。

FIRST_BUILD_CLARIFY_STEP = {
    "tool_calls": [("start_build", {"requirements_summary": "需求共识：按用户描述实现。"})]
}
"""伪模型脚本步：澄清轮直接调 start_build 产出共识（跳过问答）。"""


def parse_sse(resp) -> list[dict]:
    """解析 SSE 流为事件列表（data: JSON 行）。"""
    return [
        json.loads(line.removeprefix("data: "))
        for line in resp.iter_lines()
        if line.startswith("data: ")
    ]


def confirm_first_build(client, headers, project_id, feedback: str = "") -> list[dict]:
    """确认需求共识，工程师随即生成；返回生成流的 SSE 事件列表。"""
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/consensus/confirm",
        json={"feedback": feedback},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def seed_project_files(app, project_id, files: dict[str, str] | None = None) -> None:
    """预置项目文件（磁盘 + 索引）：模拟已有文件场景，消息直接走迭代分流。"""
    files = files if files is not None else {"index.html": "v1"}
    root = Path(app.state.settings.storage_root) / "projects" / str(project_id)
    root.mkdir(parents=True, exist_ok=True)
    with app.state.session_factory() as session:
        for path, content in files.items():
            (root / path).write_text(content, encoding="utf-8")
            session.add(
                ProjectFile(
                    project_id=project_id, path=path, size=len(content.encode("utf-8"))
                )
            )
        session.commit()
