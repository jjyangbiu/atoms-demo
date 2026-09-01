"""团队模式测试（工单 0010 历史兼容 + 模式基础）。

工单 0016 起，新团队项目改走"需求澄清 → 需求规格"流水线（见 test_team_spec），
旧"产品经理产 PRD"生成路径退役。本文件只保留：
- 团队模式的创建与展示基础
- 历史团队项目（已有 PRD 消息）的引导、确认与迭代兼容路径
- 克隆等已有文件场景跳过前置阶段
任何测试不得调用真实 MiniMax API。
"""

import json

from conftest import use_fake_model
from test_generation import _project_dir, _stream_messages
from test_projects import _create_project

from app.models import Message

PRD_TEXT = "# PRD\n\n## 目标\n做一个番茄钟。"


def _seed_legacy_prd(app, project_id, content: str = PRD_TEXT):
    """注入首条诉求与已有 PRD 消息，模拟工单 0010 旧流程下建立的历史团队项目。"""
    with app.state.session_factory() as session:
        session.add(Message(project_id=project_id, role="user", kind="text", content="做一个番茄钟"))
        session.add(Message(project_id=project_id, role="pm", kind="prd", content=content))
        session.commit()


def _confirm(client, headers, project_id, feedback=""):
    return client.post(
        f"/api/projects/{project_id}/prd/confirm",
        json={"feedback": feedback},
        headers=headers,
    )


class TestTeamModeBasics:
    def test_create_team_project_mode_visible(self, client, auth_headers):
        project = _create_project(client, auth_headers, mode="team")
        assert project["mode"] == "team"
        resp = client.get(f"/api/projects/{project['id']}", headers=auth_headers)
        assert resp.json()["mode"] == "team"


class TestLegacyPrdGuidance:
    def test_plain_message_before_confirm_is_guided_without_model_call(
        self, app, client, auth_headers
    ):
        model = use_fake_model(app, [])
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])
        events = _stream_messages(client, auth_headers, project["id"], "先把按钮做大一点")

        # 未确认前普通消息不触发任何模型调用：只有一段引导文本与 done
        assert len(model.received_messages) == 0
        types = [e["type"] for e in events]
        assert "tool" not in types
        guidance = "".join(e.get("content", "") for e in events if e["type"] == "text")
        assert "PRD" in guidance

        # 引导落对话历史，重新打开可回看
        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        texts = [m["content"] for m in resp.json() if m["kind"] == "text"]
        assert "先把按钮做大一点" in texts
        assert any("PRD" in t for t in texts if t != "先把按钮做大一点")


class TestLegacyPrdConfirm:
    def test_confirm_starts_engineer_with_prd_in_context(
        self, app, settings, client, auth_headers
    ):
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>番茄钟</h1>"})]},
                {"text": "已按 PRD 完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])

        with client.stream(
            "POST",
            f"/api/projects/{project['id']}/prd/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        ) as resp:
            assert resp.status_code == 200, resp.read()
            events = [
                json.loads(line.removeprefix("data: "))
                for line in resp.iter_lines()
                if line.startswith("data: ")
            ]

        types = [e["type"] for e in events]
        assert "tool" in types and types[-1] == "done"
        # 文件真实落盘，快照留档，对话以工程师总结收尾
        assert (
            _project_dir(settings, project["id"]) / "index.html"
        ).read_text(encoding="utf-8") == "<h1>番茄钟</h1>"
        snap = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers)
        assert len(snap.json()) == 1

        # PRD 进入了工程师的上下文
        engineer_call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in engineer_call]
        assert any(PRD_TEXT in c for c in contents)

        # 确认与 PRD 都在对话历史里
        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        kinds = [m["kind"] for m in resp.json()]
        assert "prd" in kinds and "prd_confirm" in kinds

    def test_confirm_with_feedback_reaches_engineer(self, app, client, auth_headers):
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])
        _confirm(client, auth_headers, project["id"], feedback="界面要深色主题")

        engineer_call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in engineer_call]
        assert any("界面要深色主题" in c for c in contents)

    def test_iteration_after_confirm_same_as_engineer_mode(
        self, app, settings, client, auth_headers
    ):
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])
        _confirm(client, auth_headers, project["id"])
        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done"
        assert (
            _project_dir(settings, project["id"]) / "index.html"
        ).read_text(encoding="utf-8") == "v2"

    def test_confirm_engineer_mode_project_rejected(self, app, client, auth_headers):
        use_fake_model(app, [{"text": "ok"}])
        project = _create_project(client, auth_headers)  # 默认 engineer
        assert _confirm(client, auth_headers, project["id"]).status_code == 409

    def test_confirm_without_pending_prd_rejected(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])
        assert _confirm(client, auth_headers, project["id"]).status_code == 200
        # 已确认过、没有新的待确认 PRD（锁外前置检查直接 409）
        assert _confirm(client, auth_headers, project["id"]).status_code == 409

    def test_confirm_recheck_inside_lock_rejects_second_generation(
        self, app, client, auth_headers, monkeypatch
    ):
        """锁内复查兜底：若状态在外层检查与拿到锁之间被另一路请求改变，
        本路以 error 事件收尾，不会重复生成（工单 0010 评审项）。
        用 _prd_state 先返回 pending 后返回 confirmed 模拟该竞态窗口。"""
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])

        import app.routers.projects as projects_router

        states = iter(["pending", "confirmed"])

        def flaky_state(db, project_id):
            return next(states, "confirmed")

        monkeypatch.setattr(projects_router, "_prd_state", flaky_state)

        with client.stream(
            "POST",
            f"/api/projects/{project['id']}/prd/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        ) as resp:
            assert resp.status_code == 200
            events = [
                json.loads(line.removeprefix("data: "))
                for line in resp.iter_lines()
                if line.startswith("data: ")
            ]
        assert events[-1]["type"] == "error" and "待确认" in events[-1]["detail"]
        # 工程师生成从未启动：脚本里剩余两步未被消费，也没有文件落盘与确认消息落库
        assert len(model.script) == 2
        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        assert "prd_confirm" not in [m["kind"] for m in resp.json()]


class TestExistingFilesSkipStages:
    def test_team_project_with_existing_files_skips_prd_stage(
        self, app, client, auth_headers
    ):
        """克隆等场景：团队模式项目已有文件时，首条消息直接进工程师。"""
        use_fake_model(app, [{"text": "已按诉求修改。"}])
        project = _create_project(client, auth_headers, mode="team")
        # 预置文件索引（模拟克隆得到的带文件项目；分流判断以索引为准）
        from app.models import ProjectFile

        with app.state.session_factory() as session:
            session.add(ProjectFile(project_id=project["id"], path="index.html", size=2))
            session.commit()

        events = _stream_messages(client, auth_headers, project["id"], "改一下标题")
        types = [e["type"] for e in events]
        assert "prd" not in types and "spec" not in types  # 不触发任何前置阶段
        assert types[-1] == "done"
