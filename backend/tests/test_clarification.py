"""工程师模式需求澄清与共识确认门端到端测试（工单 0015 / ADR 0003）。

验收要点：
- 首条消息进入澄清问答而非生成代码，问答全程落对话历史，刷新可回看
- 澄清智能体无法调用文件工具，唯一出口是携带需求摘要的 start_build
- 用户明确说"直接生成/跳过澄清"时立即产出共识（逃生门）
- 需求共识以卡片事件呈现，确认后才触发工程师生成；待确认时发消息重新澄清、新共识取代旧共识
- 首建流水线整体只扣 1 个名额；生成完成后的迭代消息恢复按次计数
- 已有文件的项目（含克隆）不触发澄清，行为与现状一致
任何测试不得调用真实 MiniMax API。
"""

from pathlib import Path

from conftest import FIRST_BUILD_CLARIFY_STEP, parse_sse, seed_project_files, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project


class FakeClock:
    """可控时钟：注入限流器验证名额语义（与 test_rate_limit 同构）。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _confirm(client, headers, project_id, feedback: str = "", expected_status: int = 200):
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/consensus/confirm",
        json={"feedback": feedback},
        headers=headers,
    ) as resp:
        if resp.status_code != expected_status:
            return resp.status_code, None
        return resp.status_code, parse_sse(resp)


class TestClarifyRouting:
    def test_first_message_asks_questions_instead_of_generating(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                {"text": "❓ Q1 - 配色：深色还是浅色？\n➤ 推荐：深色"},
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>时钟</h1>"})]},
                {"text": "构建完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个时钟应用")

        # 首条消息只澄清不生码：无工具事件、无共识、不落任何文件
        assert all(e["type"] in ("text", "thinking", "done") for e in events), events
        assert "".join(e["content"] for e in events if e["type"] == "text").startswith("❓ Q1")
        pdir = Path(settings.storage_root) / "projects" / str(project["id"])
        assert not (pdir / "index.html").exists()

        # 问答落对话历史，刷新可回看
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert messages[0]["role"] == "user" and messages[0]["content"] == "做一个时钟应用"
        assert messages[-1]["role"] == "clarifier" and messages[-1]["kind"] == "text"
        assert "Q1" in messages[-1]["content"]

        # 澄清续轮收敛出共识（仍不扣新名额、不写文件）
        events = _stream_messages(client, auth_headers, project["id"], "深色")
        assert any(e["type"] == "consensus" for e in events)
        assert not (pdir / "index.html").exists()

    def test_clarifier_cannot_use_file_tools(self, app, settings, client, auth_headers):
        model = use_fake_model(
            app,
            [
                # 澄清智能体尝试写文件：被拒绝并回传错误，唯一出口仍是 start_build
                {"tool_calls": [("write_file", {"path": "index.html", "content": "bad"})]},
                FIRST_BUILD_CLARIFY_STEP,
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个页面")

        # 澄清阶段对用户只呈现问答与共识：工具事件不外发，文件不落盘
        assert all(e["type"] != "tool" for e in events)
        assert any(e["type"] == "consensus" for e in events)
        pdir = Path(settings.storage_root) / "projects" / str(project["id"])
        assert not (pdir / "index.html").exists()
        # 未知工具的错误回传给了模型（循环得以继续收敛）
        tool_replies = [
            m for call in model.received_messages for m in call if type(m).__name__ == "ToolMessage"
        ]
        assert any("未知工具" in m.content for m in tool_replies)

    def test_escape_hatch_generates_directly(self, app, client, auth_headers):
        use_fake_model(app, [FIRST_BUILD_CLARIFY_STEP])
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "别问了，直接生成")
        consensus = [e for e in events if e["type"] == "consensus"]
        assert consensus and events[-1]["type"] == "done"


class TestConsensusGate:
    def test_confirm_triggers_engineer_generation(self, app, settings, client, auth_headers):
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>时钟</h1>"})]},
                {"text": "构建完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个时钟应用")

        status, events = _confirm(client, auth_headers, project["id"])
        assert status == 200
        assert events[-1]["type"] == "done"
        assert (
            Path(settings.storage_root) / "projects" / str(project["id"]) / "index.html"
        ).exists()

        # 工程师的上下文里有需求共识与确认消息
        call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in call]
        assert any("需求共识" in c for c in contents)
        assert "确认共识，开始生成。" in contents

        # 确认与共识都落对话历史，可回看
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        kinds = [m["kind"] for m in messages]
        assert "consensus" in kinds and "consensus_confirm" in kinds

    def test_confirm_with_feedback_passes_it_to_engineer(self, app, client, auth_headers):
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "红"})]},
                {"text": "构建完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个页面")
        status, events = _confirm(client, auth_headers, project["id"], feedback="主色改成红色")
        assert status == 200 and events[-1]["type"] == "done"
        # 修改意见随确认一并交给工程师（以用户消息呈现）
        call = model.received_messages[-1]
        assert any(getattr(m, "content", "") == "主色改成红色" for m in call)

    def test_message_during_pending_reclarifies_and_supersedes(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                {"tool_calls": [("start_build", {"requirements_summary": "共识一：浅色主题。"})]},
                {"tool_calls": [("start_build", {"requirements_summary": "共识二：深色主题。"})]},
                {"tool_calls": [("write_file", {"path": "index.html", "content": "深色"})]},
                {"text": "构建完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        first = _stream_messages(client, auth_headers, project["id"], "做一个页面")
        assert next(e for e in first if e["type"] == "consensus")["content"] == "共识一：浅色主题。"

        # 共识待确认时继续发消息 = 追加输入：重新澄清产出新共识
        second = _stream_messages(client, auth_headers, project["id"], "改成深色主题")
        assert next(e for e in second if e["type"] == "consensus")["content"] == "共识二：深色主题。"
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert [m["kind"] for m in messages].count("consensus") == 2

        # 确认以最新共识为准并触发生成
        status, events = _confirm(client, auth_headers, project["id"])
        assert status == 200 and events[-1]["type"] == "done"

    def test_confirm_without_pending_consensus_is_409(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        resp = client.post(
            f"/api/projects/{project['id']}/consensus/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_confirm_twice_is_409(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "ok"})]},
                {"text": "构建完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个页面")
        status, _events = _confirm(client, auth_headers, project["id"])
        assert status == 200
        resp = client.post(
            f"/api/projects/{project['id']}/consensus/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_team_mode_project_has_no_consensus_flow(self, app, client, auth_headers):
        project = _create_project(client, auth_headers, mode="team")
        resp = client.post(
            f"/api/projects/{project['id']}/consensus/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409


class TestQuotaSemantics:
    def test_first_build_pipeline_consumes_single_quota(self, app, client, auth_headers):
        """一次名额管到底（ADR 0003）：澄清续轮与共识确认都不再计数，有文件后恢复按次。"""
        clock = FakeClock()
        app.state.rate_limiter.clock = clock
        app.state.rate_limiter.per_user_hourly = 1
        use_fake_model(
            app,
            [
                {"text": "❓ Q1 - 配色？\n➤ 推荐：深色"},
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "ok"})]},
                {"text": "构建完成。"},
                {"text": "迭代完成。"},
            ],
        )
        project = _create_project(client, auth_headers)

        # 首条消息扣掉唯一名额；澄清续轮与共识确认不再计数
        _stream_messages(client, auth_headers, project["id"], "做一个页面")
        events = _stream_messages(client, auth_headers, project["id"], "深色")
        assert any(e["type"] == "consensus" for e in events)
        status, events = _confirm(client, auth_headers, project["id"])
        assert status == 200 and events[-1]["type"] == "done"

        # 首建完成（有文件）后恢复按次计数：名额已用完，迭代被拒
        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "再改改"},
            headers=auth_headers,
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["reason"] == "user_hourly"

        # 窗口过期后迭代放行
        clock.advance(3601)
        events = _stream_messages(client, auth_headers, project["id"], "再改改")
        assert events[-1]["type"] == "done"


class TestExistingFilesSkipClarify:
    def test_project_with_files_goes_straight_to_engineer(self, app, client, auth_headers):
        """已有文件（含克隆）不触发澄清：消息直接进工程师生成，行为与现状一致。"""
        use_fake_model(
            app,
            [
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"])
        events = _stream_messages(client, auth_headers, project["id"], "改一下标题")

        assert all(e["type"] != "consensus" for e in events)
        assert any(e["type"] == "tool" and e["name"] == "edit_file" for e in events)
        assert events[-1]["type"] == "done"
