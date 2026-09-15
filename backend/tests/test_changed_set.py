"""改动集权威来源测试（工单 0022 / ADR 0005「权威来源归位」段）。

两个接缝（工单 0022 预定）：
- 沙箱单元：FileSandbox 记录本轮真实改动的文件集（与「本轮已读文件集」同构）；
  编辑失败、被闸门拒绝、零 diff 空操作均不计入。
- fake 模型集成：留档快照、迭代日志、无改动警告三处硬闸由轮前/轮后磁盘指纹
  比对驱动——权威来源是磁盘状态，不再解析流式事件的参数字段。
任何测试不得调用真实 MiniMax API。
"""

import pytest
from conftest import seed_project_files, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project

from app.agent.tools import FileSandbox, SandboxViolation
from app.models import Project


@pytest.fixture
def sandbox(tmp_path):
    return FileSandbox(tmp_path)


def _seed(sandbox: FileSandbox, path: str, content: str) -> None:
    """直接落盘并模拟模型已读取（满足 read-before-write 约束）。"""
    target = sandbox.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    sandbox.read_file(path)


class TestSandboxModifiedSet:
    """沙箱侧辅助记录：本轮真实改动的文件集。"""

    def test_successful_write_file_recorded(self, sandbox):
        sandbox.write_file("index.html", "<h1>hi</h1>")
        assert sandbox.modified_this_round == {"index.html"}

    def test_successful_edit_recorded(self, sandbox):
        _seed(sandbox, "index.html", "<h1>a</h1>")
        sandbox.edit_file("index.html", "<h1>a</h1>", "<h1>b</h1>")
        assert sandbox.modified_this_round == {"index.html"}

    def test_failed_edit_not_recorded(self, sandbox):
        """old_text 未命中的失败编辑不计入改动集。"""
        _seed(sandbox, "index.html", "<h1>a</h1>")
        with pytest.raises(SandboxViolation):
            sandbox.edit_file("index.html", "<h2>不存在</h2>", "x")
        assert sandbox.modified_this_round == set()

    def test_ambiguous_edit_not_recorded(self, sandbox):
        _seed(sandbox, "index.html", "<p>a</p><p>a</p>")
        with pytest.raises(SandboxViolation):
            sandbox.edit_file("index.html", "<p>a</p>", "<p>b</p>")
        assert sandbox.modified_this_round == set()

    def test_noop_edit_not_recorded(self, sandbox):
        """零 diff 空操作被拒绝，不计入改动集。"""
        _seed(sandbox, "index.html", "<h1>a</h1>")
        with pytest.raises(SandboxViolation):
            sandbox.edit_file("index.html", "<h1>a</h1>", "<h1>a</h1>")
        assert sandbox.modified_this_round == set()

    def test_edit_without_read_not_recorded(self, sandbox):
        """被 read-before-write 闸门拒绝的编辑不计入改动集。"""
        target = sandbox.root / "index.html"
        target.write_text("<h1>a</h1>", encoding="utf-8")
        with pytest.raises(SandboxViolation):
            sandbox.edit_file("index.html", "<h1>a</h1>", "<h1>b</h1>")
        assert sandbox.modified_this_round == set()

    def test_overwrite_existing_via_write_not_recorded(self, sandbox):
        """被「禁止覆写已有文件」闸门拒绝的 write_file 不计入改动集。"""
        _seed(sandbox, "index.html", "<h1>a</h1>")
        with pytest.raises(SandboxViolation):
            sandbox.write_file("index.html", "<h1>b</h1>")
        assert sandbox.modified_this_round == set()

    def test_multi_file_mixed_only_successes_recorded(self, sandbox):
        """多文件混合：成功两笔计入，失败一笔不计入。"""
        _seed(sandbox, "index.html", "v1")
        _seed(sandbox, "styles.css", "body{}")
        sandbox.edit_file("index.html", "v1", "v2")
        with pytest.raises(SandboxViolation):
            sandbox.edit_file("styles.css", "不存在", "x")
        sandbox.write_file("app.js", "console.log(1)")
        assert sandbox.modified_this_round == {"index.html", "app.js"}

    def test_subdir_path_recorded_as_posix_relative(self, sandbox):
        sandbox.write_file("assets/img/logo.svg", "<svg/>")
        assert sandbox.modified_this_round == {"assets/img/logo.svg"}


# --- fake 模型集成：三处硬闸由磁盘指纹比对驱动 ---


def _load_log(app, project_id) -> list:
    with app.state.session_factory() as session:
        return list(session.get(Project, project_id).iteration_log)


class TestChangedSetDrivesGates:
    """留档快照 / 迭代日志 / 无改动警告全部以磁盘真实改动为准。"""

    def test_mixed_round_counts_only_real_disk_changes(
        self, app, settings, client, auth_headers
    ):
        """成功编辑 + 失败编辑 + 零 diff 空操作混合：改动集只含磁盘真改了的文件。"""
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
                    ]
                },
                {"tool_calls": [("read_file", {"path": "styles.css"})]},
                # 失败编辑：old_text 未命中
                {
                    "tool_calls": [
                        ("edit_file", {"path": "styles.css", "old_text": "不存在", "new_text": "x"})
                    ]
                },
                # 零 diff 空操作：被沙箱拒绝
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v2", "new_text": "v2"})
                    ]
                },
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(
            app, project["id"], {"index.html": "v1", "styles.css": "body{}"}
        )
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        # 迭代日志：只记磁盘真实改动的 index.html
        log = _load_log(app, project["id"])
        assert log[-1]["files"] == ["index.html"]
        # 无改动警告不触发（磁盘确有改动）
        done_events = [e for e in events if e["type"] == "done"]
        assert done_events and "warning" not in done_events[-1]
        # 留档快照照建（有真实改动）
        resp = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers)
        assert len(resp.json()) == 1

    def test_all_edits_failed_round_is_no_change(
        self, app, settings, client, auth_headers
    ):
        """工具调了但全失败：磁盘零改动 → 警告 + no_change + 不建快照。"""
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "不存在", "new_text": "x"})
                    ]
                },
                {"text": "本轮说明。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        done_events = [e for e in events if e["type"] == "done"]
        assert done_events, "应以 done 事件收尾"
        assert done_events[-1].get("warning") == "本轮未产生任何文件改动"
        assert done_events[-1].get("no_change") is True
        log = _load_log(app, project["id"])
        assert log[-1]["files"] == []
        # 零改动轮不留档（硬闸）
        resp = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers)
        assert resp.json() == []

    def test_multi_file_round_records_all_changed_files(
        self, app, settings, client, auth_headers
    ):
        """多文件成功改动：改动集含全部真实改动文件（排序后入日志）。"""
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
                    ]
                },
                {"tool_calls": [("read_file", {"path": "styles.css"})]},
                {
                    "tool_calls": [
                        (
                            "edit_file",
                            {
                                "path": "styles.css",
                                "old_text": "body{}",
                                "new_text": "body{color:red}",
                            },
                        )
                    ]
                },
                {"text": "两个文件都已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(
            app, project["id"], {"index.html": "v1", "styles.css": "body{}"}
        )
        _stream_messages(client, auth_headers, project["id"], "都改一下")

        log = _load_log(app, project["id"])
        assert log[-1]["files"] == ["index.html", "styles.css"]
