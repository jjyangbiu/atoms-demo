"""团队模式两阶段生成端到端测试（工单 0010）。

验收要点：
- 创建项目可选团队模式，模式在项目信息中可见
- 团队模式首条消息由产品经理智能体流式产出 PRD（SSE prd 事件），不写文件
- 未确认 PRD 前发送普通消息会被引导先处理 PRD（不调用模型）
- 确认（可附意见）后工程师智能体才开始生成，PRD 进入其上下文
- 确认后迭代体验与工程师模式一致
- PRD、确认与意见全部持久化于对话历史
任何测试不得调用真实 MiniMax API。
"""

import json

from conftest import use_fake_model
from test_generation import _project_dir, _stream_messages
from test_projects import _create_project

PRD_TEXT = "# PRD\n\n## 目标\n做一个番茄钟。"


def _prd_script():
    """PM 阶段脚本：一步产出 PRD 文本。"""
    return [{"text": PRD_TEXT}]


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

    def test_first_message_streams_prd_and_writes_no_files(
        self, app, settings, client, auth_headers
    ):
        use_fake_model(app, _prd_script())
        project = _create_project(client, auth_headers, mode="team")
        events = _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")

        # PRD 以 prd 事件流式产出，以 done 收尾；全程没有工具事件（PM 不动文件）
        types = [e["type"] for e in events]
        assert "prd" in types and types[-1] == "done"
        assert "tool" not in types
        assert "".join(e["content"] for e in events if e["type"] == "prd") == PRD_TEXT

        pdir = _project_dir(settings, project["id"])
        assert not pdir.exists() or not any(pdir.iterdir())

    def test_prd_persisted_in_history(self, app, client, auth_headers):
        use_fake_model(app, _prd_script())
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")

        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        messages = resp.json()
        assert messages[0]["role"] == "user" and messages[0]["content"] == "做一个番茄钟"
        prds = [m for m in messages if m["kind"] == "prd"]
        assert len(prds) == 1
        assert prds[0]["role"] == "pm" and prds[0]["content"] == PRD_TEXT


    def test_prd_stage_failure_surfaces_single_error_event(self, app, settings, client, auth_headers):
        settings.agent_max_retries = 1
        use_fake_model(app, [RuntimeError, RuntimeError])
        project = _create_project(client, auth_headers, mode="team")
        events = _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")

        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1 and "模型调用失败" in errors[0]["detail"]
        # 失败的 PRD 不落历史；重发仍可重试（无待确认卡片阻塞）
        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        assert [m["kind"] for m in resp.json()] == ["text"]


class TestPrdGuidance:
    def test_plain_message_before_confirm_is_guided_without_model_call(
        self, app, client, auth_headers
    ):
        model = use_fake_model(app, _prd_script())
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        events = _stream_messages(client, auth_headers, project["id"], "先把按钮做大一点")

        # 未确认前普通消息不触发任何模型调用：只有一段引导文本与 done
        assert len(model.received_messages) == 1  # 仅 PRD 阶段那次
        types = [e["type"] for e in events]
        assert "tool" not in types
        guidance = "".join(e.get("content", "") for e in events if e["type"] == "text")
        assert "PRD" in guidance

        # 引导落对话历史，重新打开可回看
        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        texts = [m["content"] for m in resp.json() if m["kind"] == "text"]
        assert "先把按钮做大一点" in texts
        assert any("PRD" in t for t in texts if t != "先把按钮做大一点")


class TestPrdConfirm:
    def test_confirm_starts_engineer_with_prd_in_context(
        self, app, settings, client, auth_headers
    ):
        model = use_fake_model(
            app,
            _prd_script()
            + [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>番茄钟</h1>"})]},
                {"text": "已按 PRD 完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")

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
            _prd_script()
            + [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm(client, auth_headers, project["id"], feedback="界面要深色主题")

        engineer_call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in engineer_call]
        assert any("界面要深色主题" in c for c in contents)

    def test_iteration_after_confirm_same_as_engineer_mode(
        self, app, settings, client, auth_headers
    ):
        use_fake_model(
            app,
            _prd_script()
            + [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
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
            _prd_script()
            + [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        assert _confirm(client, auth_headers, project["id"]).status_code == 200
        # 已确认过、没有新的待确认 PRD（锁外前置检查直接 409）
        assert _confirm(client, auth_headers, project["id"]).status_code == 409

    def test_confirm_recheck_inside_lock_rejects_second_generation(
        self, app, client, auth_headers, monkeypatch
    ):
        """锁内复查兑底：若状态在外层检查与拿到锁之间被另一路请求改变，
        本路以 error 事件收尾，不会重复生成（工单 0010 评审项）。
        用 _prd_state 先返回 pending 后返回 confirmed 模拟该竞态窗口。"""
        model = use_fake_model(
            app,
            _prd_script()
            + [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")

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

    def test_team_project_with_existing_files_skips_prd_stage(
        self, app, client, auth_headers
    ):
        """克隆等场景：团队模式项目已有文件时，首条消息直接进工程师。"""
        use_fake_model(
            app,
            [
                # 首个项目走完整工程师流程，产出文件
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "完成。"},
                # 第二个（模拟带文件的团队项目）直接工程师
                {"text": "已按诉求修改。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        # 预置文件索引（模拟克隆得到的带文件项目；PM 阶段判断以索引为准）
        from app.models import ProjectFile

        with app.state.session_factory() as session:
            session.add(ProjectFile(project_id=project["id"], path="index.html", size=2))
            session.commit()

        events = _stream_messages(client, auth_headers, project["id"], "改一下标题")
        types = [e["type"] for e in events]
        assert "prd" not in types  # 不触发 PRD 阶段
        assert types[-1] == "done"
