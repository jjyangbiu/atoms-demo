"""版本快照与回滚端到端测试（工单 0007）。

验收要点：
- 每次成功生成（首轮与迭代）自动留档一版快照，历史按序可见；失败不留档
- 回滚后当前文件恢复为该版本状态，后续迭代以其为基线
- 快照保留上限内清理最旧版本（连同留档文件）
- 快照存放区对智能体与文件索引不可见
任何测试不得调用真实 MiniMax API。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from conftest import (
    FIRST_BUILD_CLARIFY_STEP,
    confirm_first_build,
    login,
    use_fake_model,
)
from test_generation import _stream_messages
from test_projects import _create_project


def _project_dir(settings, project_id) -> Path:
    return Path(settings.storage_root) / "projects" / str(project_id)


def _generate(client, headers, project_id, script, text="第几版都行"):
    use_fake_model(client.app, script)
    events = _stream_messages(client, headers, project_id, text)
    # 首建分流（工单 0015）：新项目首条消息先走澄清；产出共识则确认后拿到生成事件，
    # 澄清失败（无共识）则原样返回澄清流事件。已有消息的项目直接走迭代。
    if any(e["type"] == "consensus" for e in events):
        return confirm_first_build(client, headers, project_id)
    return events


class TestSnapshotCreation:
    def test_successful_generation_creates_sequential_snapshots(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]}, {"text": "ok"}
            ],
        )
        _generate(
            client, auth_headers, project["id"],
            [{"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]}, {"text": "ok"}],
        )

        resp = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers)
        assert resp.status_code == 200
        snaps = resp.json()
        # 历史按序可见：接口返回最新版本在前，rev 连续递增
        assert [s["rev"] for s in snaps] == [2, 1]
        assert all(s["file_count"] == 1 for s in snaps)
        assert all("created_at" in s for s in snaps)

    def test_failed_generation_creates_no_snapshot(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        # 首建澄清阶段耗尽重试：失败同样不留档（工单 0015）
        events = _generate(client, auth_headers, project["id"], [RuntimeError, RuntimeError, RuntimeError])
        assert events[-1]["type"] == "error"

        resp = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers)
        assert resp.json() == []

    def test_snapshot_detail_lists_archived_files(self, app, settings, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"tool_calls": [("write_file", {"path": "styles.css", "content": "body{}"})]},
                {"text": "ok"},
            ],
        )
        snaps = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()
        resp = client.get(
            f"/api/projects/{project['id']}/snapshots/{snaps[0]['id']}", headers=auth_headers
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert [f["path"] for f in detail["files"]] == ["index.html", "styles.css"]

        # 留档文件真实存在于快照存放区
        archive = _project_dir(settings, project["id"]) / "snapshots" / str(snaps[0]["rev"])
        assert (archive / "index.html").read_text(encoding="utf-8") == "v1"

    def test_snapshots_dir_is_invisible_to_file_index_and_agent(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        events = _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                # 智能体不得写入快照存放区
                {"tool_calls": [("write_file", {"path": "snapshots/evil.html", "content": "bad"})]},
                {"text": "ok"},
            ],
        )
        assert any(e["type"] == "tool" and e["status"] == "error" for e in events)

        files = client.get(f"/api/projects/{project['id']}/files", headers=auth_headers).json()
        assert [f["path"] for f in files] == ["index.html"], "快照留档不得污染文件索引"


class TestRollback:
    def test_rollback_restores_files_and_serves_as_baseline(self, app, settings, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"tool_calls": [("write_file", {"path": "extra.html", "content": "extra"})]},
                {"text": "ok"},
            ],
        )
        _generate(
            client, auth_headers, project["id"],
            [
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"text": "ok"},
            ],
        )
        snaps = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()
        oldest = next(s for s in snaps if s["rev"] == 1)

        resp = client.post(
            f"/api/projects/{project['id']}/snapshots/{oldest['id']}/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "v1"
        assert (pdir / "extra.html").read_text(encoding="utf-8") == "extra"
        # 文件索引同步恢复
        files = client.get(f"/api/projects/{project['id']}/files", headers=auth_headers).json()
        assert {f["path"] for f in files} == {"index.html", "extra.html"}

        # 后续迭代以回滚后的基线继续：模型读到的是 v1，并留档新版本
        events = _generate(
            client, auth_headers, project["id"],
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v3"})]},
                {"text": "ok"},
            ],
        )
        read_done = [e for e in events if e["type"] == "tool" and e.get("status") == "done" and e["name"] == "read_file"]
        assert read_done and "v1" in read_done[0]["result"]
        assert (pdir / "index.html").read_text(encoding="utf-8") == "v3"
        snaps = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()
        assert [s["rev"] for s in snaps] == [3, 2, 1]

    def test_rollback_unknown_snapshot_is_404(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        resp = client.post(f"/api/projects/{project['id']}/snapshots/99999/rollback", headers=auth_headers)
        assert resp.status_code == 404

    def test_rollback_other_projects_snapshot_is_404(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]}, {"text": "ok"}
            ],
        )
        snap_id = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()[0]["id"]

        client.post("/api/auth/register", json={"username": "eve", "password": "secret123"})
        eve_headers = {"Authorization": f"Bearer {login(client, 'eve', 'secret123')}"}
        assert client.get(f"/api/projects/{project['id']}/snapshots", headers=eve_headers).status_code == 404
        assert client.post(
            f"/api/projects/{project['id']}/snapshots/{snap_id}/rollback", headers=eve_headers
        ).status_code == 404


class TestRetention:
    def test_keeps_at_most_max_snapshots_and_cleans_oldest(self, app, settings, client, auth_headers):
        settings.snapshot_max_kept = 3
        project = _create_project(client, auth_headers)
        for i in range(1, 5):
            script = [
                {"tool_calls": [("write_file", {"path": "index.html", "content": f"v{i}"})]}, {"text": "ok"}
            ]
            if i == 1:
                script.insert(0, FIRST_BUILD_CLARIFY_STEP)
            _generate(client, auth_headers, project["id"], script, text=f"第 {i} 轮")

        snaps = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()
        assert [s["rev"] for s in snaps] == [4, 3, 2], "超出上限时最旧快照被清理"

        snapshots_dir = _project_dir(settings, project["id"]) / "snapshots"
        assert not (snapshots_dir / "1").exists(), "最旧快照的留档文件一并清理"
        assert (snapshots_dir / "2").is_dir() and (snapshots_dir / "4").is_dir()

    def test_delete_project_removes_snapshots(self, app, settings, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]}, {"text": "ok"}
            ],
        )
        assert client.delete(f"/api/projects/{project['id']}", headers=auth_headers).status_code == 204
        assert not (_project_dir(settings, project["id"]) / "snapshots").exists()


class TestFileContent:
    def test_read_file_content(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>hi</h1>"})]}, {"text": "ok"}
            ],
        )
        resp = client.get(f"/api/projects/{project['id']}/files/index.html", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "index.html" and body["content"] == "<h1>hi</h1>"

    def test_read_missing_or_reserved_file_is_404(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        assert client.get(f"/api/projects/{project['id']}/files/nope.html", headers=auth_headers).status_code == 404
        assert (
            client.get(f"/api/projects/{project['id']}/files/snapshots/1/index.html", headers=auth_headers).status_code
            == 404
        )
        assert client.get(f"/api/projects/{project['id']}/files/..%2F..%2Fetc%2Fpasswd", headers=auth_headers).status_code == 404

    def test_read_file_content_with_relative_storage_root(self, tmp_path, monkeypatch):
        """回归：生产配置常用相对路径 storage_root，内容接口不得因路径未 resolve 而 500。"""
        monkeypatch.chdir(tmp_path)
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'rel.db'}",
            storage_root="./storage",
            jwt_secret="test-secret-key-for-jwt-0123456789abcdef",
            cors_origins="http://localhost:5173",
            _env_file=None,
        )
        rel_app = create_app(settings)
        use_fake_model(
            rel_app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>rel</h1>"})]}, {"text": "ok"},
            ],
        )
        rel_client = TestClient(rel_app)
        rel_client.post("/api/auth/register", json={"username": "reluser", "password": "secret123"})
        headers = {"Authorization": f"Bearer {login(rel_client, 'reluser', 'secret123')}"}
        project = _create_project(rel_client, headers)
        _stream_messages(rel_client, headers, project["id"], "做一个页面")
        confirm_first_build(rel_client, headers, project["id"])

        resp = rel_client.get(f"/api/projects/{project['id']}/files/index.html", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["content"] == "<h1>rel</h1>"
