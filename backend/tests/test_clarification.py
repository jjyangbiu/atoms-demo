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

import json
from pathlib import Path

import pytest

from conftest import FIRST_BUILD_CLARIFY_STEP, parse_sse, seed_project_files, use_fake_model
from fake_model import FakeStreamingModel
from test_generation import _stream_messages
from test_projects import _create_project

from app.agent.loop import THINK_CLOSE, THINK_OPEN


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

    def test_team_project_without_pending_consensus_confirm_rejected(
        self, app, client, auth_headers
    ):
        """团队模式也走共识确认门（工单 0016）：尚无共识时确认被拒。"""
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


ASK_STEP = {
    "tool_calls": [
        (
            "ask_options",
            {
                "questions": '[{"question": "配色用深色还是浅色？",'
                ' "options": ["深色主题", "浅色主题"], "recommend": 0}]'
            },
        )
    ]
}
"""伪模型脚本步：澄清轮经 ask_options 产出选项式问题。"""


class TestOptionBasedClarify:
    def test_ask_options_streams_clarify_event_and_persists(self, app, settings, client, auth_headers):
        """选项式澄清：结构化问题以 clarify 事件外发、落历史可回看，全程不写文件。"""
        use_fake_model(app, [ASK_STEP])
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个时钟应用")

        clarify = [e for e in events if e["type"] == "clarify"]
        assert clarify and events[-1]["type"] == "done"
        assert all(e["type"] in ("clarify", "thinking", "done") for e in events), events
        payload = json.loads(clarify[0]["content"])
        assert payload[0]["question"] == "配色用深色还是浅色？"
        assert payload[0]["options"] == ["深色主题", "浅色主题"]
        assert payload[0]["recommend"] == 0
        pdir = Path(settings.storage_root) / "projects" / str(project["id"])
        assert not (pdir / "index.html").exists()

        # 选项卡片落对话历史，刷新可回看（kind=clarify）
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert messages[-1]["role"] == "clarifier" and messages[-1]["kind"] == "clarify"
        assert json.loads(messages[-1]["content"])[0]["options"][0] == "深色主题"

    def test_invalid_payload_sent_back_to_model(self, app, client, auth_headers):
        """选项非法（非 JSON）时错误交还模型自行修正，不中断循环、不外发坏卡片。"""
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("ask_options", {"questions": "这显然不是 JSON"})]},
                ASK_STEP,
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个页面")

        # 非法一遭不外发任何 clarify 事件；修正后的合法清单照常外发并收尾
        assert any(e["type"] == "clarify" for e in events) and events[-1]["type"] == "done"
        tool_replies = [
            m for call in model.received_messages for m in call if type(m).__name__ == "ToolMessage"
        ]
        assert any("不合法" in m.content for m in tool_replies)

    def test_clarify_card_and_answer_reach_next_round(self, app, client, auth_headers):
        """选项卡片与用户回答都进后续澄清轮上下文；答完收敛出共识。"""
        model = use_fake_model(app, [ASK_STEP, FIRST_BUILD_CLARIFY_STEP])
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个时钟应用")
        events = _stream_messages(client, auth_headers, project["id"], "第 1 题：深色主题")
        assert any(e["type"] == "consensus" for e in events)

        call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in call]
        assert any("澄清问题" in c and "深色主题" in c for c in contents)
        assert "第 1 题：深色主题" in contents

    @pytest.mark.parametrize("bad", ['[{"question": "", "options": ["a", "b"]}]', "[]", '[{"question": "q", "options": ["a", "b"], "recommend": 9}]'])
    def test_validation_rejects_malformed_payload(self, bad):
        """解析器单测：空问题/空清单/推荐越界均被拒，错误文案交还模型。"""
        from app.agent.tools import parse_clarify_payload

        questions, reason = parse_clarify_payload(bad)
        assert questions is None and reason


class TestClarifyJsonRecovery:
    """部分推理模型不调 ask_options，而是把问题 JSON 写进 content——
    甚至 JSON 开头漏进 think 块、尾部被截断。此时后端须恢复出合法
    问题清单并升级为选项卡片路径，不得把半截 JSON 渲染成裸文本气泡。"""

    def test_json_split_across_think_block_recovered_as_card(self, app, client, auth_headers):
        """复现线上 trace：think 块尾部漏出 `[{`，闭标签后只剩 JSON 残段且缺尾 `]`。"""
        app.state.model_factory = lambda _s: FakeStreamingModel(
            [
                [
                    THINK_OPEN + "让我整理问题。[{\"",
                    THINK_CLOSE
                    + 'question": "配色用深色还是浅色？", "options": ["深色主题", "浅色主题"], "recommend": 0}, '
                    + '{"question": "统计功能如何？", "options": ["基础统计", "详细统计"], "recommend": 1}',
                ]
            ]
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个页面")

        clarify = [e for e in events if e["type"] == "clarify"]
        assert clarify and events[-1]["type"] == "done", events
        payload = json.loads(clarify[0]["content"])
        assert [q["question"] for q in payload] == ["配色用深色还是浅色？", "统计功能如何？"]

        # 落库为选项卡片而非裸文本残段（前端流结束后以历史重渲染）
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert messages[-1]["kind"] == "clarify"
        assert not any(m["kind"] == "text" and 'question":' in m["content"] for m in messages)

    def test_complete_json_as_plain_text_recovered_as_card(self, app, client, auth_headers):
        """模型把完整 JSON 数组当正文输出（未跨 think）：同样恢复为卡片。"""
        full = '[{"question": "配色用深色还是浅色？", "options": ["深色主题", "浅色主题"], "recommend": 0}]'
        app.state.model_factory = lambda _s: FakeStreamingModel([[full]])
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个页面")
        assert any(e["type"] == "clarify" for e in events), events

    def test_old_style_free_text_question_not_recovered(self, app, client, auth_headers):
        """自由文本提问不含合法 JSON 清单：保持旧形态文本路径，不误升级。"""
        app.state.model_factory = lambda _s: FakeStreamingModel(
            [["❓ Q1 - 配色：深色还是浅色？\n➤ 推荐：深色"]]
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个页面")
        assert all(e["type"] != "clarify" for e in events)
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert messages[-1]["kind"] == "text"


class TestClarifyAnswerMarker:
    """弹窗式澄清（工单 0020）：弹窗提交答案携 clarify_answer 标记，
    落库 kind=clarify_answer，历史回看据此与新请求区分；无标记消息仍落 kind=text。
    标记消息在后续澄清轮仍以用户消息入上下文，分流与名额语义不变。"""

    def _send(self, client, headers, project_id, content, marker):
        with client.stream(
            "POST",
            f"/api/projects/{project_id}/messages",
            json={"content": content, "clarify_answer": marker},
            headers=headers,
        ) as resp:
            assert resp.status_code == 200, resp.read()
            return parse_sse(resp)

    def test_marker_persists_clarify_answer_kind(self, app, client, auth_headers):
        use_fake_model(app, [ASK_STEP, ASK_STEP])
        project = _create_project(client, auth_headers)
        self._send(client, auth_headers, project["id"], "做一个时钟应用", False)
        self._send(client, auth_headers, project["id"], "第 1 题：深色主题", True)

        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert [m["kind"] for m in user_msgs] == ["text", "clarify_answer"]
        assert user_msgs[1]["content"] == "第 1 题：深色主题"

    def test_without_marker_still_text_kind(self, app, client, auth_headers):
        use_fake_model(app, [ASK_STEP, ASK_STEP])
        project = _create_project(client, auth_headers)
        self._send(client, auth_headers, project["id"], "做一个时钟应用", False)
        self._send(client, auth_headers, project["id"], "配色直接用深色", False)

        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert [m["kind"] for m in user_msgs] == ["text", "text"]

    def test_marker_answer_reaches_next_round_context(self, app, client, auth_headers):
        """标记消息以用户消息入后续澄清轮上下文（语义与现状一致）。"""
        model = use_fake_model(app, [ASK_STEP, FIRST_BUILD_CLARIFY_STEP])
        project = _create_project(client, auth_headers)
        self._send(client, auth_headers, project["id"], "做一个时钟应用", False)
        events = self._send(client, auth_headers, project["id"], "第 1 题：深色主题", True)
        assert any(e["type"] == "consensus" for e in events)

        call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in call]
        assert "第 1 题：深色主题" in contents
