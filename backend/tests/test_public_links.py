"""公开链接的 nginx 直出链接维护测试（工单 0013）。

容器化部署里 /p/{slug} 静态由 nginx 直出、不经后端：
后端在 {storage_root}/p/ 下维护 slug → 项目目录的符号链接，
nginx 的 root 指向 storage_root 即可按 URI 直出（见 frontend/nginx.conf）。

验收要点：
- 发布即建立符号链接，透过链接读到的就是项目目录当前文件
- 迭代改动文件后无需重新发布，链接直通最新内容
- 取消发布 / 删除项目即移除链接（公开入口立即消失）
- 应用启动按发布记录重建链接（容器重启恢复），并清除孤立链接
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from conftest import (
    FIRST_BUILD_CLARIFY_STEP,
    confirm_first_build,
    login,
    use_fake_model,
)
from test_generation import _stream_messages
from test_projects import _create_project
from test_publish import _generate_index, _publish


def _links_root(app) -> Path:
    return Path(app.state.settings.storage_root) / "p"


class TestPublicLinkLifecycle:
    def test_publish_creates_link_readable_as_project_files(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers, content="<h1>时钟</h1>")
        pub = _publish(client, auth_headers, project["id"])

        link = _links_root(app) / pub["slug"]
        assert link.is_symlink()
        # nginx 直出语义：透过链接读到的就是项目目录的当前文件
        assert (link / "index.html").read_text(encoding="utf-8") == "<h1>时钟</h1>"

    def test_iteration_flows_through_link_without_republish(self, app, client, auth_headers):
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

        link = _links_root(app) / pub["slug"]
        assert "v2" in (link / "index.html").read_text(encoding="utf-8")

    def test_unpublish_removes_link(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        pub = _publish(client, auth_headers, project["id"])
        assert (_links_root(app) / pub["slug"]).is_symlink()

        assert (
            client.delete(f"/api/projects/{project['id']}/publish", headers=auth_headers).status_code
            == 204
        )
        assert not (_links_root(app) / pub["slug"]).exists()

    def test_delete_project_removes_link(self, app, client, auth_headers):
        project = _generate_index(app, client, auth_headers)
        pub = _publish(client, auth_headers, project["id"])
        assert client.delete(f"/api/projects/{project['id']}", headers=auth_headers).status_code == 204
        assert not (_links_root(app) / pub["slug"]).exists()


class TestPublicLinkResync:
    def test_startup_resync_restores_links_and_drops_orphans(self, settings):
        app = create_app(settings)
        client = TestClient(app)
        client.post("/api/auth/register", json={"username": "alice", "password": "secret123"})
        headers = {"Authorization": f"Bearer {login(client, 'alice', 'secret123')}"}
        project = _generate_index(app, client, headers)
        pub = _publish(client, headers, project["id"])

        links_root = Path(settings.storage_root) / "p"
        link = links_root / pub["slug"]
        assert link.is_symlink()

        # 模拟容器重启后数据卷链接丢失，另留一个无发布记录的孤立链接
        link.unlink()
        orphan = links_root / "orphan000"
        orphan.symlink_to("../projects/99999", target_is_directory=True)

        # 同一数据库再起一个实例即等价重启：链接按发布记录重建、孤立链接被清除
        create_app(settings)
        assert link.is_symlink()
        assert (link / "index.html").read_text(encoding="utf-8")
        assert not orphan.is_symlink()
