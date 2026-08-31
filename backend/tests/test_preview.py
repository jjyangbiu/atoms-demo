"""预览托管端到端测试（工单 0005）。

验收要点：
- 属主访问预览路径可看到应用真实运行（多文件相对引用正常）
- 鉴权只靠登录 Cookie：未登录 401；非属主 404
- 路径穿越防护：无法借预览通道读取项目目录之外的文件

TestClient 自动维护 Cookie 罐：auth_headers 依赖登录过后，同一 client
的后续请求天然携带 atoms_token Cookie，与浏览器行为一致。
任何测试不得调用真实 MiniMax API。
"""

from fastapi.testclient import TestClient

from conftest import use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project


def _generate_two_files(app, client, headers) -> dict:
    use_fake_model(
        app,
        [
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
    project = _create_project(client, headers)
    _stream_messages(client, headers, project["id"], "做一个时钟应用")
    return project


class TestPreview:
    def test_owner_can_preview_app_files(self, app, client, auth_headers):
        project = _generate_two_files(app, client, auth_headers)

        resp = client.get(f"/api/projects/{project['id']}/preview/index.html")
        assert resp.status_code == 200, resp.text
        assert "时钟" in resp.text
        assert resp.headers["content-type"].startswith("text/html")

        # 多文件间的相对资源引用：同目录下的 css 可被正常取到（子资源同样靠 Cookie）
        css = client.get(f"/api/projects/{project['id']}/preview/styles.css")
        assert css.status_code == 200
        assert css.text == "h1{color:red}"
        assert css.headers["content-type"].startswith("text/css")

    def test_preview_root_serves_index_html(self, app, client, auth_headers):
        project = _generate_two_files(app, client, auth_headers)
        resp = client.get(f"/api/projects/{project['id']}/preview")
        assert resp.status_code == 200
        assert "时钟" in resp.text

    def test_preview_requires_login(self, app, client, auth_headers):
        project = _generate_two_files(app, client, auth_headers)
        # 独立客户端、无登录 Cookie → 401
        fresh = TestClient(app)
        assert fresh.get(f"/api/projects/{project['id']}/preview/index.html").status_code == 401

    def test_logout_clears_preview_cookie(self, app, client, auth_headers):
        project = _generate_two_files(app, client, auth_headers)
        assert client.get(f"/api/projects/{project['id']}/preview").status_code == 200
        client.post("/api/auth/logout", headers=auth_headers)
        assert client.get(f"/api/projects/{project['id']}/preview").status_code == 401

    def test_preview_denies_non_owner(self, app, client, auth_headers):
        project = _generate_two_files(app, client, auth_headers)
        # 独立客户端登录 eve：她的 Cookie 访问他人项目应 404
        eve = TestClient(app)
        eve.post("/api/auth/register", json={"username": "eve", "password": "secret123"})
        assert (
            eve.post("/api/auth/login", json={"username": "eve", "password": "secret123"}).status_code
            == 200
        )
        assert eve.get(f"/api/projects/{project['id']}/preview/index.html").status_code == 404

    def test_preview_blocks_path_traversal(self, app, client, auth_headers):
        project = _generate_two_files(app, client, auth_headers)
        # URL 编码的 ..（绕过客户端路径规范化）不得借预览通道越出项目目录：
        # 解码后 ../../evil.html 指向 storage 根（无该文件），能取到即说明越界成功，故必须 404
        assert (
            client.get(
                f"/api/projects/{project['id']}/preview/%2e%2e/%2e%2e/evil.html"
            ).status_code
            == 404
        )
        # 沙箱内的 .. 回溯是合法相对引用，应正常解析到目标文件（多文件引用的常见形态）
        inner = client.get(f"/api/projects/{project['id']}/preview/sub/../index.html")
        assert inner.status_code == 200
        assert "时钟" in inner.text
        # 白名单之外的扩展名同样拒绝
        assert client.get(f"/api/projects/{project['id']}/preview/app.exe").status_code == 404
        # 不存在的路径
        assert client.get(f"/api/projects/{project['id']}/preview/nope.html").status_code == 404
