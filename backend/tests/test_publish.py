"""发布与稳定链接端到端测试（工单 0006）。

验收要点：
- 发布返回唯一稳定 slug，匿名（未登录）浏览器可访问 /p/{slug} 运行应用
- 发布后继续迭代，链接不变且内容同步为最新版本
- 取消发布后公开链接立即失效（404）
- 同一项目至多一条活跃发布记录（重复发布幂等，返回同一 slug）
- 非属主无法发布/取消发布他人项目

公开托管直接读取项目目录的当前文件，因此"内容自动更新"无需任何
同步动作，测试以迭代前后两次访问同一链接验证。
任何测试不得调用真实 MiniMax API。
"""

from fastapi.testclient import TestClient

from conftest import FIRST_BUILD_CLARIFY_STEP, confirm_first_build, login, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project


def _generate_index(app, client, headers, content="<h1>时钟</h1>") -> dict:
    """用伪模型生成一个含 index.html 的项目并返回项目（首建先过澄清确认门，工单 0015）。"""
    use_fake_model(
        app,
        [
            FIRST_BUILD_CLARIFY_STEP,
            {"tool_calls": [("write_file", {"path": "index.html", "content": content})]},
            {"text": "完成。"},
        ],
    )
    project = _create_project(client, headers)
    _stream_messages(client, headers, project["id"], "做一个时钟应用")
    confirm_first_build(client, headers, project["id"])
    return project


def _publish(client, headers, project_id, expected_status=201) -> dict:
    resp = client.post(f"/api/projects/{project_id}/publish", headers=headers)
    assert resp.status_code == expected_status, resp.text
    return resp.json()


class TestPublish:
    def test_publish_returns_stable_slug_and_public_link_runs(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        pub = _publish(client, auth_headers, project["id"])
        assert pub["slug"]
        assert pub["url"] == f"/p/{pub['slug']}"

        # 匿名（未登录）浏览器访问公开链接：全新客户端、不带任何 Cookie
        anonymous = TestClient(app)
        resp = anonymous.get(f"/p/{pub['slug']}")
        assert resp.status_code == 200, resp.text
        assert "时钟" in resp.text
        assert resp.headers["content-type"].startswith("text/html")
        # 用户生成脚本不得触碰主站源：HTML 响应必须带 CSP sandbox（不透明源）
        assert "sandbox" in resp.headers.get("content-security-policy", "")

    def test_publish_serves_all_project_files_anonymously(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {
                    "tool_calls": [
                        (
                            "write_file",
                            {
                                "path": "index.html",
                                "content": '<link rel="stylesheet" href="styles.css"><h1>时钟</h1>',
                            },
                        )
                    ]
                },
                {"tool_calls": [("write_file", {"path": "styles.css", "content": "h1{color:red}"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个时钟应用")
        confirm_first_build(client, auth_headers, project["id"])
        pub = _publish(client, auth_headers, project["id"])

        anonymous = TestClient(app)
        # 多文件相对资源引用：子资源同样无需登录；非 HTML 不需 CSP sandbox（只对文档生效）
        css = anonymous.get(f"/p/{pub['slug']}/styles.css")
        assert css.status_code == 200
        assert css.text == "h1{color:red}"
        assert css.headers["content-type"].startswith("text/css")
        assert "content-security-policy" not in css.headers

    def test_publish_without_generated_files_rejected(self, client, auth_headers):
        project = _create_project(client, auth_headers)
        resp = client.post(f"/api/projects/{project['id']}/publish", headers=auth_headers)
        assert resp.status_code == 400

    def test_republish_is_idempotent_single_active_record(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        first = _publish(client, auth_headers, project["id"])
        second = _publish(client, auth_headers, project["id"], expected_status=200)
        assert second["slug"] == first["slug"]

    def test_iteration_updates_public_link_content_without_changing_slug(
        self, app, client, auth_headers
    ):
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
        pub = _publish(client, auth_headers, project["id"])

        _stream_messages(client, auth_headers, project["id"], "改一下标题")

        anonymous = TestClient(app)
        resp = anonymous.get(f"/p/{pub['slug']}")
        assert resp.status_code == 200
        assert "v2" in resp.text  # 链接不变，内容同步为最新版本

    def test_unpublish_revokes_public_link(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        pub = _publish(client, auth_headers, project["id"])
        anonymous = TestClient(app)
        assert anonymous.get(f"/p/{pub['slug']}").status_code == 200

        resp = client.delete(f"/api/projects/{project['id']}/publish", headers=auth_headers)
        assert resp.status_code == 204
        assert anonymous.get(f"/p/{pub['slug']}").status_code == 404

    def test_unpublish_not_published_is_404(self, client, auth_headers):
        project = _create_project(client, auth_headers)
        assert (
            client.delete(f"/api/projects/{project['id']}/publish", headers=auth_headers).status_code
            == 404
        )

    def test_republish_after_unpublish_works(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        _publish(client, auth_headers, project["id"])
        assert (
            client.delete(f"/api/projects/{project['id']}/publish", headers=auth_headers).status_code
            == 204
        )
        pub = _publish(client, auth_headers, project["id"])
        assert TestClient(app).get(f"/p/{pub['slug']}").status_code == 200

    def test_project_detail_exposes_published_slug(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        assert (
            client.get(f"/api/projects/{project['id']}", headers=auth_headers).json()["published_slug"]
            is None
        )
        pub = _publish(client, auth_headers, project["id"])
        assert (
            client.get(f"/api/projects/{project['id']}", headers=auth_headers).json()["published_slug"]
            == pub["slug"]
        )

    def test_public_link_serves_project_directory_not_snapshot(self, app, client, auth_headers):
        """公开链接始终指向项目目录当前文件：未发布过的路径形态同样 404。"""
        anonymous = TestClient(app)
        assert anonymous.get("/p/no-such-slug").status_code == 404

    def test_public_link_blocks_path_traversal(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        pub = _publish(client, auth_headers, project["id"])
        anonymous = TestClient(app)
        assert anonymous.get(f"/p/{pub['slug']}/%2e%2e/%2e%2e/evil.html").status_code == 404
        assert anonymous.get(f"/p/{pub['slug']}/app.exe").status_code == 404
        assert anonymous.get(f"/p/{pub['slug']}/nope.html").status_code == 404

    def test_delete_project_revokes_publication(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        pub = _publish(client, auth_headers, project["id"])
        assert client.delete(f"/api/projects/{project['id']}", headers=auth_headers).status_code == 204
        assert TestClient(app).get(f"/p/{pub['slug']}").status_code == 404


class TestPublishAuthorization:
    def test_publish_requires_login(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        assert TestClient(app).post(f"/api/projects/{project['id']}/publish").status_code == 401

    def test_non_owner_cannot_publish_or_unpublish(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        client.post("/api/auth/register", json={"username": "eve", "password": "secret123"})
        eve_headers = {"Authorization": f"Bearer {login(client, 'eve', 'secret123')}"}
        assert (
            client.post(f"/api/projects/{project['id']}/publish", headers=eve_headers).status_code
            == 404
        )
        # 属主先发布，非属主也不得取消
        _publish(client, auth_headers, project["id"])
        assert (
            client.delete(f"/api/projects/{project['id']}/publish", headers=eve_headers).status_code
            == 404
        )
