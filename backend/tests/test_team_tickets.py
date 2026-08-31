"""团队模式「规格确认 → 工单拆解 → 清单确认门」端到端测试（工单 0017 / ADR 0003）。

验收要点：
- 规格确认后自动拆单，工单清单以 tickets 事件产出，不写文件
- 工单持久化于工单表（项目、序号、标题、交付内容、阻塞依赖、状态），刷新可回看
- 待确认时继续发消息视为调整粒度/内容的意见，重新拆解，新清单取代旧清单
- 清单确认后进入执行期：工程师开始实现，工单清单进入其上下文
- 确认后再发消息不触发重新澄清或拆单
- 一个名额管到底：拆单与清单确认不另计数
任何测试不得调用真实 MiniMax API。
"""

import json

from conftest import FIRST_BUILD_CLARIFY_STEP, parse_sse, use_fake_model
from test_generation import _project_dir, _stream_messages
from test_projects import _create_project

SPEC_TEXT = "# 番茄钟 需求规格\n\n## 目标\n做一个番茄钟。"

TICKETS_PAYLOAD = json.dumps(
    [
        {
            "title": "骨架页面",
            "deliverable": "index.html 打开即见番茄钟骨架，计时区占位清晰可见。",
            "blocked_by": [],
        },
        {
            "title": "计时核心",
            "deliverable": "开始/暂停/重置可用，倒计时实时跳动。",
            "blocked_by": [1],
        },
    ],
    ensure_ascii=False,
)

SECOND_TICKETS_PAYLOAD = json.dumps(
    [
        {
            "title": "骨架与计时合并交付",
            "deliverable": "页面打开即见完整计时，开始/暂停可用。",
            "blocked_by": [],
        },
        {
            "title": "统计页",
            "deliverable": "完成番茄后可查看今日统计。",
            "blocked_by": [1],
        },
    ],
    ensure_ascii=False,
)

BREAK_STEP = {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]}
"""伪模型脚本步：拆单轮直接提交工单清单。"""

SECOND_BREAK_STEP = {"tool_calls": [("submit_tickets", {"tickets": SECOND_TICKETS_PAYLOAD})]}

ENGINEER_BUILD_STEPS = [
    {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>番茄钟</h1>"})]},
    {"text": "已按工单完成。"},
]


class FakeClock:
    """可控时钟：注入限流器验证名额语义（与 test_team_spec 同构）。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _confirm_consensus(client, headers, project_id) -> list[dict]:
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/consensus/confirm",
        json={"feedback": ""},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def _confirm_spec(client, headers, project_id, feedback: str = "") -> list[dict]:
    """确认规格（团队新流水线下随即触发拆单）；返回 SSE 事件列表。"""
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/spec/confirm",
        json={"feedback": feedback},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def _confirm_tickets(client, headers, project_id, feedback: str = "") -> list[dict]:
    """确认工单清单（随即进入执行期）；返回 SSE 事件列表。"""
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/tickets/confirm",
        json={"feedback": feedback},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def _break_tickets(app, client, headers, break_step: dict = BREAK_STEP):
    """把新团队项目推进到"工单清单待确认"：澄清收敛 → 规格起草 → 规格确认触发拆单。"""
    use_fake_model(app, [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}, break_step])
    project = _create_project(client, headers, mode="team")
    _stream_messages(client, headers, project["id"], "做一个番茄钟")
    _confirm_consensus(client, headers, project["id"])
    _confirm_spec(client, headers, project["id"])
    return project


class TestTicketBreakdown:
    def test_spec_confirm_auto_breaks_down_tickets(self, app, settings, client, auth_headers):
        """规格确认后自动拆单：清单以 tickets 事件产出，全程不写文件。"""
        use_fake_model(app, [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}, BREAK_STEP])
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        events = _confirm_spec(client, auth_headers, project["id"])

        types = [e["type"] for e in events]
        assert "tickets" in types and "tool" not in types and types[-1] == "done"
        payload = json.loads(next(e["content"] for e in events if e["type"] == "tickets"))
        assert [t["title"] for t in payload] == ["骨架页面", "计时核心"]

        pdir = _project_dir(settings, project["id"])
        assert not pdir.exists() or not any(pdir.iterdir())

    def test_tickets_persisted_and_listed(self, app, client, auth_headers):
        """工单持久化于工单表，刷新可回看：历史里有清单行，清单接口返回卡片字段。"""
        project = _break_tickets(app, client, auth_headers)

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        kinds = [m["kind"] for m in messages]
        assert "spec_confirm" in kinds and "tickets" in kinds
        tickets_msg = next(m for m in messages if m["kind"] == "tickets")
        assert tickets_msg["role"] == "breaker_agent"

        tickets = client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()
        assert [(t["seq"], t["title"]) for t in tickets] == [(1, "骨架页面"), (2, "计时核心")]
        assert tickets[0]["blocked_by"] == [] and tickets[1]["blocked_by"] == [1]
        assert all(t["status"] == "open" for t in tickets)
        assert "计时" in tickets[1]["deliverable"]

    def test_invalid_payload_sent_back_to_model(self, app, client, auth_headers):
        """清单非法（非 JSON）时错误交还模型自行修正，不中断循环、不产生坏数据。"""
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": "这显然不是 JSON"})]},
                BREAK_STEP,
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        events = _confirm_spec(client, auth_headers, project["id"])

        assert events[-1]["type"] == "done"
        tickets = client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()
        assert len(tickets) == 2


class TestTicketRedraft:
    def test_message_during_pending_rebreaks_and_supersedes(
        self, app, settings, client, auth_headers
    ):
        """待确认时继续发消息 = 调整意见：重新拆解，新清单取代旧清单。"""
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                BREAK_STEP,
                SECOND_BREAK_STEP,
                *ENGINEER_BUILD_STEPS,
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        first = _confirm_spec(client, auth_headers, project["id"])
        assert next(e for e in first if e["type"] == "tickets")["content"] == TICKETS_PAYLOAD
        assert len(client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()) == 2

        second = _stream_messages(client, auth_headers, project["id"], "工单太碎了，合并成一个")
        assert next(e for e in second if e["type"] == "tickets")["content"] == SECOND_TICKETS_PAYLOAD

        # 重新拆解看到了旧清单（在其基础上调整，而不是从零再来）
        rebreak_call = model.received_messages[-1]
        assert any("骨架页面" in getattr(m, "content", "") for m in rebreak_call)

        # 新清单取代旧清单：旧两单消失，新单序号在历史最大值上续编；
        # blocked_by 同步换算为续编后的序号，引用不悬空（指向 #3 而非已删的旧单）
        tickets = client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()
        assert [(t["seq"], t["title"]) for t in tickets] == [
            (3, "骨架与计时合并交付"),
            (4, "统计页"),
        ]
        assert tickets[0]["blocked_by"] == [] and tickets[1]["blocked_by"] == [3]

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert [m["kind"] for m in messages].count("tickets") == 2

        # 确认以最新清单为准并进入执行期
        events = _confirm_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"
        assert (_project_dir(settings, project["id"]) / "index.html").exists()


class TestTicketsConfirm:
    def test_confirm_starts_engineer_with_tickets_in_context(
        self, app, settings, client, auth_headers
    ):
        """清单确认 = 执行期入口：工程师随即实现，工单清单进入其上下文。"""
        model = use_fake_model(
            app,
            [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}, BREAK_STEP, *ENGINEER_BUILD_STEPS],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        types = [e["type"] for e in events]
        assert "tool" in types and types[-1] == "done"
        assert (
            _project_dir(settings, project["id"]) / "index.html"
        ).read_text(encoding="utf-8") == "<h1>番茄钟</h1>"

        # 确认消息（携工单清单）与规格都进了工程师的上下文
        engineer_call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in engineer_call]
        assert any("请按工单清单执行" in c and "计时核心" in c for c in contents)
        assert any(SPEC_TEXT in c for c in contents)

        # 确认落对话历史，可回看
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert "tickets_confirm" in [m["kind"] for m in messages]

    def test_confirm_with_feedback_reaches_engineer(self, app, client, auth_headers):
        model = use_fake_model(
            app,
            [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}, BREAK_STEP, *ENGINEER_BUILD_STEPS],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        _confirm_tickets(client, auth_headers, project["id"], feedback="第二个工单加上统计页")

        engineer_call = model.received_messages[-1]
        assert any(
            getattr(m, "content", "").startswith("第二个工单加上统计页") for m in engineer_call
        )

    def test_message_after_confirmed_no_rebreak_no_reclarify(
        self, app, settings, client, auth_headers
    ):
        """确认后冻结：再发消息不触发重新澄清/拆单，直接由工程师承接迭代。"""
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                BREAK_STEP,
                *ENGINEER_BUILD_STEPS,
                {"text": "已调整。"},
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        _confirm_tickets(client, auth_headers, project["id"])

        events = _stream_messages(client, auth_headers, project["id"], "把按钮改大一点")
        types = [e["type"] for e in events]
        # 不再是澄清提问、共识卡片或拆单，而是普通生成收尾
        assert types[-1] == "done"
        assert "consensus" not in types and "tickets" not in types
        text = "".join(e.get("content", "") for e in events if e["type"] == "text")
        assert "❓" not in text

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert [m["kind"] for m in messages].count("tickets") == 1

    def test_confirm_without_pending_is_409(self, app, client, auth_headers):
        project = _create_project(client, auth_headers, mode="team")
        resp = client.post(
            f"/api/projects/{project['id']}/tickets/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_confirm_twice_is_409(self, app, client, auth_headers):
        use_fake_model(
            app,
            [FIRST_BUILD_CLARIFY_STEP, {"text": SPEC_TEXT}, BREAK_STEP, *ENGINEER_BUILD_STEPS],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        assert _confirm_tickets(client, auth_headers, project["id"])[-1]["type"] == "done"
        resp = client.post(
            f"/api/projects/{project['id']}/tickets/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_engineer_mode_project_confirm_tickets_rejected(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)  # 默认 engineer
        resp = client.post(
            f"/api/projects/{project['id']}/tickets/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 409


class TestTeamTicketsQuota:
    def test_pipeline_consumes_single_quota(self, app, client, auth_headers):
        """一次名额管到底（ADR 0003）：拆单与清单确认不另计数，有文件后恢复按次。"""
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

        # 首条消息扣掉唯一名额；后续澄清/规格/拆单/清单确认都不再计数
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
