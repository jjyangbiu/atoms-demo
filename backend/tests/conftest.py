"""测试公共骨架：临时 SQLite + 临时存储目录 + TestClient。

这是后续所有测试的范本（见工单 0002 与规格 Testing Decisions）：
- 只测外部可观察行为（HTTP 响应），不测内部实现
- 任何测试不得依赖外部服务（真实 LLM / 向量库）
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from fake_model import FakeModel


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_root=str(tmp_path / "storage"),
        jwt_secret="test-secret-key-for-jwt-0123456789abcdef",
        cors_origins="http://localhost:5173",
        _env_file=None,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


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
