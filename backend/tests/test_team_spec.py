"""团队模式「需求澄清 → 需求规格」流水线端到端测试（工单 0016 / ADR 0003）。

验收要点：
- 新团队项目首条消息进入需求澄清，不再产出 PRD（旧流程退役）
- 需求共识确认后，规格智能体产出需求规格卡片（SSE spec 事件），不写文件，落历史
- 规格待确认时继续发消息视为修改意见，重新起草，新规格取代旧规格
- 规格确认后拆单智能体随即开始拆解（工单 0017），规格进入其上下文
- 历史团队项目的 PRD 展示、确认与只读路径不受影响
任何测试不得调用真实 MiniMax API。
"""

from conftest import FIRST_BUILD_CLARIFY_STEP, parse_sse, use_fake_model
from test_generation import _project_dir, _stream_messages
from test_projects import _create_project
from test_team_tickets import BREAK_STEP, _confirm_tickets

from app.models import Message

SPEC_TEXT = "# 番茄钟 需求规格\n\n## 目标\n做一个番茄钟。"

ENGINEER_BUILD_STEPS = [
    {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>番茄钟</h1>"})]},
    {"text": "已按规格完成。"},
]


class FakeClock:
    """可控时钟：注入限流器验证名额语义（与 test_clarification 同构）。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _confirm_consensus(client, headers, project_id, feedback: str = "") -> list[dict]:
    """确认需求共识（团队模式下随即起草需求规格）；返回 SSE 事件列表。"""
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/consensus/confirm",
        json={"feedback": feedback},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def _confirm_spec(client, headers, project_id, feedback: str = "") -> list[dict]:
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/spec/confirm",
        json={"feedback": feedback},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def _draft_spec(app, client, headers, extra_steps: list | None = None, spec_text: str = SPEC_TEXT):
    """把新团队项目推进到"规格待确认"：澄清收敛 → 共识确认 → 规格起草。"""
    use_fake_model(
        app, [FIRST_BUILD_CLARIFY_STEP, {"text": spec_text}, *(extra_steps or [])]
    )
    project = _create_project(client, headers, mode="team")
    _stream_messages(client, headers, project["id"], "做一个番茄钟")
    return project


def _seed_legacy_prd(app, project_id: int, content: str = "# 番茄钟 PRD\n\n## 目标\n做一个番茄钟。"):
    """注入已有 PRD 消息，模拟工单 0010 旧流程下建立的历史团队项目。"""
    with app.state.session_factory() as session:
        session.add(Message(project_id=project_id, role="user", kind="text", content="做一个番茄钟"))
        session.add(Message(project_id=project_id, role="pm", kind="prd", content=content))
        session.commit()


class TestSpecProduction:
    def test_new_team_project_first_message_goes_to_clarify_not_prd(
        self, app, client, auth_headers
    ):
        """旧"产 PRD"流程对新团队项目退役：首条消息进澄清，无任何 prd 事件与消息。"""
        use_fake_model(app, [{"text": "❓ Q1 - 配色：深色还是浅色？\n➤ 推荐：深色"}])
        project = _create_project(client, auth_headers, mode="team")
        events = _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")

        types = [e["type"] for e in events]
        assert "prd" not in types and "spec" not in types
        assert "".join(e["content"] for e in events if e["type"] == "text").startswith("❓ Q1")
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert all(m["kind"] != "prd" for m in messages)
        assert messages[-1]["role"] == "clarifier"

    def test_consensus_confirm_streams_spec_card_and_writes_no_files(
        self, app, settings, client, auth_headers
    ):
        use_fake_model(app, [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}])
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        events = _confirm_consensus(client, auth_headers, project["id"])

        # 规格以 spec 事件流式产出、以 done 收尾；全程没有工具事件（规格智能体不动文件）
        types = [e["type"] for e in events]
        assert "spec" in types and "tool" not in types and types[-1] == "done"
        assert "".join(e["content"] for e in events if e["type"] == "spec") == SPEC_TEXT

        pdir = _project_dir(settings, project["id"])
        assert not pdir.exists() or not any(pdir.iterdir())

    def test_spec_persisted_in_history(self, app, client, auth_headers):
        project = _draft_spec(app, client, auth_headers)
        _confirm_consensus(client, auth_headers, project["id"])

        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        kinds = [m["kind"] for m in messages]
        assert "consensus" in kinds and "consensus_confirm" in kinds and "spec" in kinds
        spec = next(m for m in messages if m["kind"] == "spec")
        assert spec["role"] == "spec_agent" and spec["content"] == SPEC_TEXT


class TestSpecRedraft:
    def test_message_during_pending_redrafts_and_supersedes(
        self, app, settings, client, auth_headers
    ):
        """规格待确认时继续发消息 = 修改意见：重新起草，新规格取代旧规格。"""
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": "规格一：浅色主题。"},
                {"text": "规格二：深色主题。"},
                BREAK_STEP,
                *ENGINEER_BUILD_STEPS,
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        first = _confirm_consensus(client, auth_headers, project["id"])
        assert next(e for e in first if e["type"] == "spec")["content"] == "规格一：浅色主题。"

        second = _stream_messages(client, auth_headers, project["id"], "界面改成深色主题")
        assert next(e for e in second if e["type"] == "spec")["content"] == "规格二：深色主题。"
        # 重新起草看到了旧规格（在其基础上改，而不是从零再来）
        redraft_call = model.received_messages[-1]
        assert any("规格一" in getattr(m, "content", "") for m in redraft_call)

        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert [m["kind"] for m in messages].count("spec") == 2

        # 确认以最新规格为准：先拆单，再确认清单进入执行期（工单 0017）
        events = _confirm_spec(client, auth_headers, project["id"])
        assert any(e["type"] == "tickets" for e in events)
        events = _confirm_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"
        assert (_project_dir(settings, project["id"]) / "index.html").exists()


class TestSpecConfirm:
    def test_confirm_starts_breaker_with_spec_in_context(
        self, app, client, auth_headers
    ):
        """规格确认 = 拆单入口（工单 0017）：拆单智能体随即拆解，规格进入其上下文。"""
        model = use_fake_model(
            app, [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}, BREAK_STEP]
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        events = _confirm_spec(client, auth_headers, project["id"])

        types = [e["type"] for e in events]
        assert "tickets" in types and "tool" not in types and types[-1] == "done"

        # 规格与确认消息都进了拆单智能体的上下文
        breaker_call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in breaker_call]
        assert any(SPEC_TEXT in c for c in contents)
        assert "确认规格，开始拆解工单。" in contents

        # 规格与确认都落对话历史，可回看
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        kinds = [m["kind"] for m in messages]
        assert "spec" in kinds and "spec_confirm" in kinds

    def test_confirm_with_feedback_reaches_breaker(self, app, client, auth_headers):
        model = use_fake_model(
            app, [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}, BREAK_STEP]
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"], feedback="界面要深色主题")

        breaker_call = model.received_messages[-1]
        assert any(getattr(m, "content", "") == "界面要深色主题" for m in breaker_call)

    def test_confirm_without_pending_spec_is_409(self, app, client, auth_headers):
        project = _create_project(client, auth_headers, mode="team")
        resp = client.post(
            f"/api/projects/{project['id']}/spec/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_confirm_twice_is_409(self, app, client, auth_headers):
        project = _draft_spec(app, client, auth_headers, extra_steps=[BREAK_STEP])
        _confirm_consensus(client, auth_headers, project["id"])
        assert _confirm_spec(client, auth_headers, project["id"])[-1]["type"] == "done"
        resp = client.post(
            f"/api/projects/{project['id']}/spec/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_engineer_mode_project_confirm_spec_rejected(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)  # 默认 engineer
        resp = client.post(
            f"/api/projects/{project['id']}/spec/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409


class TestLegacyPrdCompatibility:
    """历史团队项目（已有 PRD 消息）的展示、确认与只读路径保持兼容。"""

    def test_legacy_pending_prd_still_guided_without_model_call(
        self, app, client, auth_headers
    ):
        model = use_fake_model(app, [])
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])
        events = _stream_messages(client, auth_headers, project["id"], "先把按钮做大一点")

        # 待确认 PRD 的历史项目：引导文案不变，且不调用模型
        assert len(model.received_messages) == 0
        guidance = "".join(e.get("content", "") for e in events if e["type"] == "text")
        assert "PRD" in guidance

    def test_legacy_prd_confirm_still_triggers_engineer(
        self, app, settings, client, auth_headers
    ):
        model = use_fake_model(app, ENGINEER_BUILD_STEPS)
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])
        with client.stream(
            "POST",
            f"/api/projects/{project['id']}/prd/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        ) as resp:
            assert resp.status_code == 200, resp.read()
            events = parse_sse(resp)

        assert events[-1]["type"] == "done"
        assert (_project_dir(settings, project["id"]) / "index.html").exists()
        # PRD 仍在历史里可见（只读路径兼容）
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        kinds = [m["kind"] for m in messages]
        assert "prd" in kinds and "prd_confirm" in kinds
        # 工程师上下文里有历史 PRD
        engineer_call = model.received_messages[-1]
        assert any("PRD" in getattr(m, "content", "") for m in engineer_call)

    def test_legacy_prd_readonly_visible_in_history(self, app, client, auth_headers):
        project = _create_project(client, auth_headers, mode="team")
        _seed_legacy_prd(app, project["id"])
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        prds = [m for m in messages if m["kind"] == "prd"]
        assert len(prds) == 1 and prds[0]["role"] == "pm"


class TestTeamPipelineQuota:
    def test_team_pipeline_consumes_single_quota(self, app, client, auth_headers):
        """一次名额管到底（ADR 0003）：澄清、共识确认、规格起草与规格确认都不再计数，
        有文件后恢复按次。"""
        clock = FakeClock()
        app.state.rate_limiter.clock = clock
        app.state.rate_limiter.per_user_hourly = 1
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                BREAK_STEP,
                *ENGINEER_BUILD_STEPS,
                {"text": "迭代完成。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")

        # 首条消息扣掉唯一名额；后续流水线阶段不再计数（拆单与清单确认见 test_team_tickets）
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        events = _confirm_consensus(client, auth_headers, project["id"])
        assert any(e["type"] == "spec" for e in events)
        events = _confirm_spec(client, auth_headers, project["id"])
        assert any(e["type"] == "tickets" for e in events)
        events = _confirm_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"

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
