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
            [{"tool_calls": [("read_file", {"path": "index.html"})]}, {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]}, {"text": "ok"}],
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
                {"tool_calls": [("read_file", {"path": "index.html"})]},
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
            if i == 1:
                script = [
                    FIRST_BUILD_CLARIFY_STEP,
                    {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]}, {"text": "ok"},
                ]
            else:
                # 后续轮必须产生真实改动才建快照（零改动轮不留档是硬闸语义）：先读后改
                script = [
                    {"tool_calls": [("read_file", {"path": "index.html"})]},
                    {"tool_calls": [("edit_file", {"path": "index.html", "old_text": f"v{i - 1}", "new_text": f"v{i}"})]},
                    {"text": "ok"},
                ]
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


class TestSnapshotDiff:
    """差异端点（Layer 6 安全网）：某版相对前一版的文件级改动，供用户核对“只改了该改的”。"""

    def test_first_snapshot_diff_marks_all_added(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "ok"},
            ],
        )
        snap = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()[0]
        resp = client.get(
            f"/api/projects/{project['id']}/snapshots/{snap['id']}/diff", headers=auth_headers
        )
        assert resp.status_code == 200
        diff = resp.json()
        # 首版无基线：base_rev 为 None，全部文件标 added
        assert diff["base_rev"] is None and diff["target_rev"] == 1
        assert [(f["path"], f["status"]) for f in diff["files"]] == [("index.html", "added")]
        assert diff["files"][0]["old"] == "" and diff["files"][0]["new"] == "v1"

    def test_diff_shows_only_changed_files_between_revs(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        # 首版：index.html + styles.css
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"tool_calls": [("write_file", {"path": "styles.css", "content": "body{}"})]},
                {"text": "ok"},
            ],
        )
        # 迭代：只改 index.html、新增 app.js；styles.css 不动
        _generate(
            client, auth_headers, project["id"],
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"tool_calls": [("write_file", {"path": "app.js", "content": "// js"})]},
                {"text": "ok"},
            ],
        )
        snaps = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()
        rev2 = next(s for s in snaps if s["rev"] == 2)
        diff = client.get(
            f"/api/projects/{project['id']}/snapshots/{rev2['id']}/diff", headers=auth_headers
        ).json()
        assert diff["base_rev"] == 1 and diff["target_rev"] == 2
        by_path = {f["path"]: f for f in diff["files"]}
        # 未改动的 styles.css 不出现；改动/新增按状态标注（旧新内容齐备）
        assert set(by_path) == {"index.html", "app.js"}
        assert by_path["index.html"]["status"] == "modified"
        assert by_path["index.html"]["old"] == "v1" and by_path["index.html"]["new"] == "v2"
        assert by_path["app.js"]["status"] == "added" and by_path["app.js"]["new"] == "// js"

    def test_diff_other_projects_snapshot_is_404(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        _generate(
            client, auth_headers, project["id"],
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]}, {"text": "ok"},
            ],
        )
        snap_id = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers).json()[0]["id"]
        client.post("/api/auth/register", json={"username": "eve_diff", "password": "secret123"})
        eve_headers = {"Authorization": f"Bearer {login(client, 'eve_diff', 'secret123')}"}
        assert client.get(
            f"/api/projects/{project['id']}/snapshots/{snap_id}/diff", headers=eve_headers
        ).status_code == 404


def test_diff_snapshot_detects_removed_file(tmp_path):
    """diff_snapshot 的 removed 分支：基线有、目标无的文件标 removed（生成无删除工具，直接验辅助函数）。"""
    from types import SimpleNamespace

    from app.snapshots import diff_snapshot, snapshots_root

    # 手工铺两个快照留档：rev1 有 a.html + b.html，rev2 只剩 a.html（内容也改了）
    for rev, files in [(1, {"a.html": "A", "b.html": "B"}), (2, {"a.html": "A2"})]:
        d = snapshots_root(tmp_path) / str(rev)
        d.mkdir(parents=True)
        for name, content in files.items():
            (d / name).write_text(content, encoding="utf-8")

    changes = {c["path"]: c for c in diff_snapshot(tmp_path, SimpleNamespace(rev=1), SimpleNamespace(rev=2))}
    assert changes["a.html"]["status"] == "modified"
    assert changes["a.html"]["old"] == "A" and changes["a.html"]["new"] == "A2"
    assert changes["b.html"]["status"] == "removed"
    assert changes["b.html"]["old"] == "B" and changes["b.html"]["new"] == ""
