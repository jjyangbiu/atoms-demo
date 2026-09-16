"""团队模式「工单清单确认 → 检查点串行执行」端到端测试（工单 0018 / ADR 0003）。

验收要点：
- 工单按依赖（序号）顺序串行执行，SSE 推送开始/完成/失败进度事件
- 每个工单完成形成一个检查点快照，融入现有快照体系（列表、预览、回滚入口可用）
- 单个工单失败可重试，从该工单起点重来，已完成的工单不重跑
- 断线重连后可看到执行状态（工单状态 + 进度行）并从断点继续
- 整个首建流水线（含执行、失败重试、继续）只占 1 个名额，全部完成后迭代按次计数
任何测试不得调用真实 MiniMax API。
"""

import json

from conftest import FIRST_BUILD_CLARIFY_STEP, _turn_result_step, parse_sse, use_fake_model
from test_generation import _project_dir, _stream_messages
from test_projects import _create_project
from test_team_tickets import (
    SPEC_TEXT,
    TICKETS_PAYLOAD,
    FakeClock,
    _confirm_consensus,
    _confirm_spec,
)

TICKET1_STEPS = [
    {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>骨架</h1>"})]},
    _turn_result_step(summary="骨架页面已完成。", changed_files=["index.html"]),
]
TICKET2_STEPS = [
    {"tool_calls": [("write_file", {"path": "timer.js", "content": "// 计时核心"})]},
    _turn_result_step(summary="计时核心已完成。", changed_files=["timer.js"]),
]

# 工单 2 的模型调用失败：重试三步全抛（未外发内容时每步都会重试，见 loop.run_generation），
# 耗尽后以 error 收尾，正好模拟单张工单执行失败。失败的 ainvoke 也计入 received_messages。
TICKET2_FAIL_STEPS = [RuntimeError("模型服务抖动")] * 3

CONFIRM_EXEC_SCRIPT = [
    FIRST_BUILD_CLARIFY_STEP,
    {"text": SPEC_TEXT},
    {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
    *TICKET1_STEPS,
    *TICKET2_STEPS,
]

# --- 零交付闸脚本步（工单 0029）：两种「磁盘零改动」的非法出口与一种合法逃逸 ---

PROSE_EXIT_STEPS = [
    {"text": "骨架页面全部搞定了。"},
    {"text": "真的搞定了。"},
]
"""伪模型脚本步：工单轮以两段散文收尾、始终未走唯一出口，且磁盘零写入（兜底 + 零改动）。"""

VERBAL_CLAIM_STEPS = [
    _turn_result_step(summary="骨架页面已完成。", changed_files=["index.html"]),
    _turn_result_step(summary="骨架页面已完成。", changed_files=["index.html"]),
]
"""伪模型脚本步：申报改了 index.html 但磁盘零写入——自洽性回喂一次后仍申报（verbal_completion 放行）。"""

NO_CHANGE_STEP = _turn_result_step(
    intent="no_change",
    summary="骨架已存在。",
    changed_files=[],
    no_change_reason="前次执行已完成本单交付，磁盘现状即满足交付内容。",
)
"""伪模型脚本步：磁盘零改动的结构化 no_change——须经工单级确认回喂后第二次提交才放行。"""


def _confirm_tickets(client, headers, project_id, feedback: str = "") -> list[dict]:
    """确认工单清单（随即进入检查点串行执行）；返回 SSE 事件列表。"""
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/tickets/confirm",
        json={"feedback": feedback},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def _resume_tickets(client, headers, project_id) -> list[dict]:
    """继续/重试工单执行（从第一张未完成工单起点）；返回 SSE 事件列表。"""
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/tickets/resume",
        json={},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp)


def _break_and_confirm(app, client, headers, script: list):
    """把新团队项目推进过清单确认：澄清收敛 → 规格 → 拆单 → 确认（触发执行）。"""
    use_fake_model(app, script)
    project = _create_project(client, headers, mode="team")
    _stream_messages(client, headers, project["id"], "做一个番茄钟")
    _confirm_consensus(client, headers, project["id"])
    _confirm_spec(client, headers, project["id"])
    return project


class TestSerialExecution:
    def test_tickets_execute_serially_with_progress_events(self, app, settings, client, auth_headers):
        """清单确认即串行执行：逐单开始/完成进度事件，单内指令只含当前工单。"""
        model = use_fake_model(app, CONFIRM_EXEC_SCRIPT)
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        progress = [e for e in events if e["type"] == "ticket_progress"]
        assert [(p["seq"], p["status"]) for p in progress] == [
            (1, "running"),
            (1, "done"),
            (2, "running"),
            (2, "done"),
        ]
        # 整体进度随完成数递增
        assert [p["done"] for p in progress] == [0, 1, 1, 2]
        assert all(p["total"] == 2 for p in progress)
        assert events[-1]["type"] == "done"
        # 两单各自的交付文件都落盘
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").exists() and (pdir / "timer.js").exists()

        # 执行顺序：工单 1 的指令先于工单 2；每次调用只要求执行当前这一单，
        # 不提前执行后面的工单（指令里附清单划定边界，但执行目标只有一单）
        exec_instructions = []
        for call in model.received_messages:
            for m in call:
                content = getattr(m, "content", "")
                if "现在执行工单" in content:
                    exec_instructions.append(content)
                    break
        first_t1 = next(i for i, c in enumerate(exec_instructions) if "现在执行工单 1" in c)
        first_t2 = next(i for i, c in enumerate(exec_instructions) if "现在执行工单 2" in c)
        assert first_t1 < first_t2
        assert "现在执行工单 2" not in exec_instructions[first_t1]

    def test_each_ticket_forms_checkpoint_snapshot(self, app, settings, client, auth_headers):
        """每单完成形成一个检查点快照：工单表记录引用，快照列表可见，内容可分版回看。"""
        use_fake_model(app, CONFIRM_EXEC_SCRIPT)
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"

        tickets = client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()
        assert [t["status"] for t in tickets] == ["done", "done"]
        assert [t["snapshot_rev"] for t in tickets] == [1, 2]

        snapshots = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert [s["rev"] for s in snapshots] == [2, 1]
        # 检查点 1 只有骨架，检查点 2 才有计时核心——版本可区分、可回滚
        detail1 = client.get(
            f"/api/projects/{project['id']}/snapshots/{snapshots[1]['id']}", headers=auth_headers
        ).json()
        assert [f["path"] for f in detail1["files"]] == ["index.html"]
        detail2 = client.get(
            f"/api/projects/{project['id']}/snapshots/{snapshots[0]['id']}", headers=auth_headers
        ).json()
        assert sorted(f["path"] for f in detail2["files"]) == ["index.html", "timer.js"]

    def test_progress_and_results_survive_refresh(self, app, client, auth_headers):
        """执行过程持久化：进度行与每单结论入对话历史，刷新可回看。"""
        use_fake_model(app, CONFIRM_EXEC_SCRIPT)
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        assert _confirm_tickets(client, auth_headers, project["id"])[-1]["type"] == "done"

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        ticket_rows = [m for m in messages if m["kind"] == "ticket"]
        assert len(ticket_rows) == 2
        payloads = [json.loads(m["content"]) for m in ticket_rows]
        assert [(p["seq"], p["status"]) for p in payloads] == [(1, "done"), (2, "done")]
        assert payloads[0]["snapshot_rev"] == 1 and payloads[1]["snapshot_rev"] == 2
        # 每单的工程师结论也在历史里：以轮次产物卡片留痕（工单 0024/0025，不再有纯文本行）
        cards = [
            json.loads(m["content"])
            for m in messages
            if m["kind"] == "turn_result" and m["role"] == "engineer"
        ]
        summaries = [c["summary"] for c in cards]
        assert "骨架页面已完成。" in summaries and "计时核心已完成。" in summaries


class TestFailureAndRetry:
    def test_failure_stops_at_ticket_and_retry_from_it(
        self, app, settings, client, auth_headers
    ):
        """工单 2 失败：停在失败单；重试从该单起点，工单 1 绝不重跑。"""
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
                *TICKET1_STEPS,
                *TICKET2_FAIL_STEPS,
                *TICKET2_STEPS,
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        progress = [e for e in events if e["type"] == "ticket_progress"]
        assert [(p["seq"], p["status"]) for p in progress] == [
            (1, "running"),
            (1, "done"),
            (2, "running"),
            (2, "failed"),
        ]
        assert events[-1]["type"] == "error" and "工单 2" in events[-1]["detail"]
        # 检查点只到工单 1：失败单不留档
        tickets = client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()
        assert [t["status"] for t in tickets] == ["done", "failed"]
        assert [t["snapshot_rev"] for t in tickets] == [1, None]

        calls_before_retry = len(model.received_messages)
        events = _resume_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"
        progress = [e for e in events if e["type"] == "ticket_progress"]
        assert [(p["seq"], p["status"]) for p in progress] == [(2, "running"), (2, "done")]

        # 重试只跑了工单 2 的一次执行（写文件 + 收尾两次模型调用）：工单 1 未重跑，
        # 不产生工单 1 的新检查点（快照版本号在 1 之后只新增了一个）
        retry_calls = model.received_messages[calls_before_retry:]
        assert len(retry_calls) == 2
        assert any("现在执行工单 2" in getattr(m, "content", "") for m in retry_calls[0])
        tickets = client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()
        assert [t["status"] for t in tickets] == ["done", "done"]
        assert [t["snapshot_rev"] for t in tickets] == [1, 2]
        assert (_project_dir(settings, project["id"]) / "timer.js").exists()

    def test_send_message_during_failure_is_intercepted(self, app, client, auth_headers):
        """执行期（有失败待重试）发普通消息：引导先重试，不调模型不计名额。"""
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
                *TICKET1_STEPS,
                *TICKET2_FAIL_STEPS,
                *TICKET2_STEPS,  # 重试/继续时需有得跑：引导拦截不消耗脚本步
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        assert _confirm_tickets(client, auth_headers, project["id"])[-1]["type"] == "error"

        calls_before = len(model.received_messages)
        events = _stream_messages(client, auth_headers, project["id"], "帮帮我")
        assert events[-1]["type"] == "done"
        text = "".join(e.get("content", "") for e in events if e["type"] == "text")
        assert "工单 2 执行失败" in text and "重试" in text
        assert len(model.received_messages) == calls_before  # 引导不调模型

        # 引导不落名额痕迹后仍可正常重试
        events = _resume_tickets(client, auth_headers, project["id"])
        assert [
            (p["seq"], p["status"]) for p in events if p["type"] == "ticket_progress"
        ] == [(2, "running"), (2, "done")]

    def test_resume_without_active_execution_is_409(self, app, client, auth_headers):
        project = _create_project(client, auth_headers, mode="team")
        resp = client.post(f"/api/projects/{project['id']}/tickets/resume", json={}, headers=auth_headers)
        assert resp.status_code == 409

    def test_resume_after_all_done_is_409(self, app, client, auth_headers):
        use_fake_model(app, CONFIRM_EXEC_SCRIPT)
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        assert _confirm_tickets(client, auth_headers, project["id"])[-1]["type"] == "done"
        resp = client.post(f"/api/projects/{project['id']}/tickets/resume", json={}, headers=auth_headers)
        assert resp.status_code == 409


class TestZeroDeliverableGate:
    """工单 0029：磁盘零改动收尾的工单轮不得静默标 done。

    唯一的零改动合法出口是经工单级确认回喂后的结构化 no_change（如「交付已在前次
    中断轮完成」的重跑轮）：该出口照常 done + 检查点，但进度行显式标注「零改动收尾」
    交人工判断；散文兜底收尾与 modify_code 申报零改动（verbal_completion 放行）
    一律按执行失败处置——不建快照、不建卡片、不标 done，/tickets/resume 可从该单重试。
    """

    def test_prose_fallback_zero_change_fails_and_retries(
        self, app, settings, client, auth_headers
    ):
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
                *PROSE_EXIT_STEPS,
                *TICKET1_STEPS,
                *TICKET2_STEPS,
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        progress = [e for e in events if e["type"] == "ticket_progress"]
        assert [(p["seq"], p["status"]) for p in progress] == [
            (1, "running"),
            (1, "failed"),
        ]
        assert events[-1]["type"] == "error"
        assert "零改动收尾" in events[-1]["detail"]
        assert "可从该工单重试" in events[-1]["detail"]
        tickets = client.get(
            f"/api/projects/{project['id']}/tickets", headers=auth_headers
        ).json()
        assert [t["status"] for t in tickets] == ["failed", "open"]
        # 失败单不留检查点：零改动、无成果可留档
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []

        # resume 从该单重试：这次真实交付，两单全部 done
        events = _resume_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"
        tickets = client.get(
            f"/api/projects/{project['id']}/tickets", headers=auth_headers
        ).json()
        assert [t["status"] for t in tickets] == ["done", "done"]
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").exists() and (pdir / "timer.js").exists()

    def test_verbal_completion_zero_change_fails(self, app, client, auth_headers):
        """宣称改了文件但磁盘零写入：自洽性回喂救不回来时，零交付闸按失败处置。"""
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
                *VERBAL_CLAIM_STEPS,
                *TICKET1_STEPS,
                *TICKET2_STEPS,
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        assert (1, "failed") in [
            (p["seq"], p["status"]) for p in events if p["type"] == "ticket_progress"
        ]
        assert events[-1]["type"] == "error" and "零改动收尾" in events[-1]["detail"]

        events = _resume_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"
        tickets = client.get(
            f"/api/projects/{project['id']}/tickets", headers=auth_headers
        ).json()
        assert [t["status"] for t in tickets] == ["done", "done"]

    def test_confirmed_no_change_closes_with_annotation(self, app, client, auth_headers):
        """合法逃逸：确认回喂后仍 no_change → done，但进度行标注「零改动收尾」。"""
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
                NO_CHANGE_STEP,
                NO_CHANGE_STEP,
                *TICKET2_STEPS,
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        assert events[-1]["type"] == "done"
        progress = [e for e in events if e["type"] == "ticket_progress"]
        done1 = next(p for p in progress if p["seq"] == 1 and p["status"] == "done")
        done2 = next(p for p in progress if p["seq"] == 2 and p["status"] == "done")
        assert done1["zero_change"] is True
        assert "zero_change" not in done2

        # 确认回喂确实到达模型（轮级预算一次，房规同自洽性核验）
        assert any(
            "磁盘零改动" in getattr(m, "content", "")
            for call in model.received_messages
            for m in call
        )

        tickets = client.get(
            f"/api/projects/{project['id']}/tickets", headers=auth_headers
        ).json()
        assert [t["status"] for t in tickets] == ["done", "done"]

        # 标注随进度行落库：刷新回看同样能识别「零改动收尾」
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        rows = [json.loads(m["content"]) for m in messages if m["kind"] == "ticket"]
        seq1_done = next(r for r in rows if r["seq"] == 1 and r["status"] == "done")
        assert seq1_done["zero_change"] is True


class TestExecQuota:
    def test_execution_and_retry_share_single_quota(self, app, client, auth_headers):
        """一次名额管到底：串行执行与失败重试都在首建名额内，全部完成后迭代按次。"""
        clock = FakeClock()
        app.state.rate_limiter.clock = clock
        app.state.rate_limiter.per_user_hourly = 1
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
                *TICKET1_STEPS,
                *TICKET2_FAIL_STEPS,
                *TICKET2_STEPS,
                _turn_result_step(
                    intent="no_change",
                    summary="迭代完成。",
                    no_change_reason="本轮仅确认，无文件改动。",
                ),
            ],
        )
        project = _create_project(client, auth_headers, mode="team")

        # 首条消息扣掉唯一名额；其后的确认与执行（含工单 1 成功产出文件）都不再计数
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        assert _confirm_tickets(client, auth_headers, project["id"])[-1]["type"] == "error"

        # 失败重试不重复占名额：尽管项目已有文件（工单 1 的检查点）
        events = _resume_tickets(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"
        tickets = client.get(f"/api/projects/{project['id']}/tickets", headers=auth_headers).json()
        assert all(t["status"] == "done" for t in tickets)

        # 全部完成后进入常规迭代：名额已用完，迭代消息被拒
        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "再改改"},
            headers=auth_headers,
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["reason"] == "user_hourly"

        # 窗口过期后迭代放行（迭代消息此时才计数：首建名额早已用完）
        clock.advance(3601)
        events = _stream_messages(client, auth_headers, project["id"], "再改改")
        assert events[-1]["type"] == "done"
