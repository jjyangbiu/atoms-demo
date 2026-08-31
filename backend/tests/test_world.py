"""App 世界画廊与克隆端到端测试（工单 0008）。

验收要点：
- 匿名（未登录）可浏览画廊列表与详情：标题、描述、作者、实时运行预览链接
- 画廊只收录已发布应用；未发布项目不出现
- 注册用户一键克隆：新项目归属克隆者、文件完整、可立即对话迭代
- 未登录克隆返回 401（前端据此引导登录/注册）
- 克隆与发布解耦：克隆项目不带公开链接，原项目后续演进/下架不影响克隆副本

任何测试不得调用真实 MiniMax API。
"""

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import FIRST_BUILD_CLARIFY_STEP, confirm_first_build, login, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project


def _generate_and_publish(
    app, client, headers, name="时钟", prompt="做一个时钟应用", content="<h1>时钟</h1>"
) -> tuple[dict, dict]:
    """用伪模型生成一个项目并发布，返回 (项目, 发布结果)。首建先过澄清确认门（工单 0015）。"""
    use_fake_model(
        app,
        [
            FIRST_BUILD_CLARIFY_STEP,
            {"tool_calls": [("write_file", {"path": "index.html", "content": content})]},
            {"text": "完成。"},
        ],
    )
    project = _create_project(client, headers, name=name)
    _stream_messages(client, headers, project["id"], prompt)
    confirm_first_build(client, headers, project["id"])
    resp = client.post(f"/api/projects/{project['id']}/publish", headers=headers)
    assert resp.status_code == 201, resp.text
    return project, resp.json()


def _register_and_login(client, username: str) -> dict:
    """注册并登录一个新用户，返回请求头。"""
    assert (
        client.post("/api/auth/register", json={"username": username, "password": "secret123"})
        .status_code
        == 201
    )
    return {"Authorization": f"Bearer {login(client, username, 'secret123')}"}


class TestWorldGallery:
    def test_world_lists_published_apps_anonymously(self, app, client, auth_headers):
        project, pub = _generate_and_publish(app, client, auth_headers)

        # 匿名（未登录）浏览器访问画廊：全新客户端、不带任何 Cookie
        anonymous = TestClient(app)
        resp = anonymous.get("/api/world")
        assert resp.status_code == 200, resp.text
        apps = resp.json()
        assert len(apps) == 1
        entry = apps[0]
        assert entry["slug"] == pub["slug"]
        assert entry["title"] == project["name"]
        assert entry["description"] == "做一个时钟应用"  # 首个用户诉求即应用描述
        assert entry["author"] == "alice"
        assert entry["preview_url"] == f"/p/{pub['slug']}"
        assert entry["published_at"]

        # 详情页同样匿名可访问
        detail = anonymous.get(f"/api/world/{pub['slug']}")
        assert detail.status_code == 200
        assert detail.json() == entry

    def test_world_excludes_unpublished_projects(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>私</h1>"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个不发布的应用")
        confirm_first_build(client, auth_headers, project["id"])

        assert TestClient(app).get("/api/world").json() == []

    def test_world_unpublished_after_publish_disappears(self, app, client, auth_headers):
        project, pub = _generate_and_publish(app, client, auth_headers)
        assert client.delete(f"/api/projects/{project['id']}/publish", headers=auth_headers).status_code == 204
        assert TestClient(app).get("/api/world").json() == []

    def test_world_sorted_newest_first(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>v</h1>"})]},
                {"text": "完成。"},
            ]
            * 2,
        )
        first = _create_project(client, auth_headers, name="先发布")
        _stream_messages(client, auth_headers, first["id"], "第一个")
        confirm_first_build(client, auth_headers, first["id"])
        first_pub = client.post(f"/api/projects/{first['id']}/publish", headers=auth_headers).json()
        second = _create_project(client, auth_headers, name="后发布")
        _stream_messages(client, auth_headers, second["id"], "第二个")
        confirm_first_build(client, auth_headers, second["id"])
        second_pub = client.post(f"/api/projects/{second['id']}/publish", headers=auth_headers).json()

        titles = [a["title"] for a in TestClient(app).get("/api/world").json()]
        assert titles == ["后发布", "先发布"]
        assert first_pub["slug"] != second_pub["slug"]

    def test_world_detail_unknown_slug_is_404(self, client):
        assert client.get("/api/world/no-such-slug").status_code == 404


class TestClone:
    def test_clone_creates_owned_project_with_full_files(self, app, client, auth_headers):
        _, pub = _generate_and_publish(app, client, auth_headers)
        eve_headers = _register_and_login(client, "eve")

        resp = client.post(f"/api/world/{pub['slug']}/clone", headers=eve_headers)
        assert resp.status_code == 201, resp.text
        cloned = resp.json()
        assert cloned["published_slug"] is None  # 克隆与发布解耦

        # 新项目归属克隆者：出现在 eve 的项目列表，不出现在原作者列表
        assert [p["id"] for p in client.get("/api/projects", headers=eve_headers).json()] == [
            cloned["id"]
        ]
        assert [p["id"] for p in client.get("/api/projects", headers=auth_headers).json()] != [
            cloned["id"]
        ]

        # 文件完整复制：清单与内容都可由克隆者读取
        files = client.get(f"/api/projects/{cloned['id']}/files", headers=eve_headers).json()
        assert [f["path"] for f in files] == ["index.html"]
        content = client.get(
            f"/api/projects/{cloned['id']}/files/index.html", headers=eve_headers
        ).json()
        assert content["content"] == "<h1>时钟</h1>"

    def test_clone_requires_login(self, app, client, auth_headers):
        _, pub = _generate_and_publish(app, client, auth_headers)
        assert TestClient(app).post(f"/api/world/{pub['slug']}/clone").status_code == 401

    def test_clone_unknown_slug_is_404(self, client, auth_headers):
        assert client.post("/api/world/no-such-slug/clone", headers=auth_headers).status_code == 404

    def test_clone_is_decoupled_from_source(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>v1</h1>"})]},
                {"text": "第一版。"},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
                    ]
                },
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个页面")
        confirm_first_build(client, auth_headers, project["id"])
        pub = client.post(f"/api/projects/{project['id']}/publish", headers=auth_headers).json()

        eve_headers = _register_and_login(client, "eve")
        cloned = client.post(f"/api/world/{pub['slug']}/clone", headers=eve_headers).json()

        # 原项目继续迭代到 v2……
        _stream_messages(client, auth_headers, project["id"], "改一下标题")
        assert TestClient(app).get(f"/p/{pub['slug']}").status_code == 200

        # ……克隆副本不受影响，仍是 v1
        content = client.get(
            f"/api/projects/{cloned['id']}/files/index.html", headers=eve_headers
        ).json()
        assert content["content"] == "<h1>v1</h1>"

        # 原项目下架乃至删除，克隆副本依然完整可用
        assert (
            client.delete(f"/api/projects/{project['id']}/publish", headers=auth_headers).status_code
            == 204
        )
        assert client.delete(f"/api/projects/{project['id']}", headers=auth_headers).status_code == 204
        assert (
            client.get(f"/api/projects/{cloned['id']}", headers=eve_headers).status_code == 200
        )
        content = client.get(
            f"/api/projects/{cloned['id']}/files/index.html", headers=eve_headers
        ).json()
        assert content["content"] == "<h1>v1</h1>"

    def test_clone_ready_for_immediate_iteration(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>v1</h1>"})]},
                {"text": "第一版。"},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
                    ]
                },
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个页面")
        confirm_first_build(client, auth_headers, project["id"])
        pub = client.post(f"/api/projects/{project['id']}/publish", headers=auth_headers).json()

        eve_headers = _register_and_login(client, "eve")
        cloned = client.post(f"/api/world/{pub['slug']}/clone", headers=eve_headers).json()

        # 克隆后立即对话迭代：智能体基于复制来的文件继续修改
        events = _stream_messages(client, eve_headers, cloned["id"], "改一下标题")
        assert events[-1]["type"] == "done"
        content = client.get(
            f"/api/projects/{cloned['id']}/files/index.html", headers=eve_headers
        ).json()
        assert content["content"] == "<h1>v2</h1>"

    def test_clone_source_files_gone_is_404_without_orphan_project(self, app, settings, client, auth_headers):
        """源目录在发布后消失（并发删除）：克隆拒绝且不留下孤儿项目。"""
        project, pub = _generate_and_publish(app, client, auth_headers)
        eve_headers = _register_and_login(client, "eve")
        shutil.rmtree(
            Path(settings.storage_root) / "projects" / str(project["id"]), ignore_errors=True
        )

        resp = client.post(f"/api/world/{pub['slug']}/clone", headers=eve_headers)
        assert resp.status_code == 404
        assert client.get("/api/projects", headers=eve_headers).json() == []

    def test_owner_can_clone_own_published_app(self, app, client, auth_headers):
        """克隆自己的应用也成立：得到独立的新项目，原项目与其链接不受影响。"""
        project, pub = _generate_and_publish(app, client, auth_headers)
        cloned = client.post(f"/api/world/{pub['slug']}/clone", headers=auth_headers).json()
        assert cloned["id"] != project["id"]
        assert client.get(f"/api/projects/{cloned['id']}", headers=auth_headers).status_code == 200
        assert TestClient(app).get(f"/p/{pub['slug']}").status_code == 200
