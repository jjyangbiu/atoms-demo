"""团队模式「检查点回滚 → 继续执行」端到端测试（工单 0019 / ADR 0003）。

验收要点：
- 回滚到某检查点后，项目文件恢复为该检查点状态，其后的工单重置为未完成
- 「继续执行」入口（/tickets/resume）仅在存在未完成工单时可用，由用户手动触发
- 续跑从第一个未完成工单开始，按依赖（序号）顺序执行并生成新的检查点
- 回滚—续跑可反复进行，状态始终一致
- 回滚与续跑不占用新的生成名额
任何测试不得调用真实 MiniMax API。
"""

from conftest import FIRST_BUILD_CLARIFY_STEP, use_fake_model
from test_generation import _project_dir, _stream_messages
from test_projects import _create_project
from test_team_exec import TICKET1_STEPS, TICKET2_FAIL_STEPS, TICKET2_STEPS, _confirm_tickets, _resume_tickets
from test_team_tickets import (
    SPEC_TEXT,
    TICKETS_PAYLOAD,
    FakeClock,
    _confirm_consensus,
    _confirm_spec,
)

PIPELINE_STEPS = [
    FIRST_BUILD_CLARIFY_STEP,
    {"text": SPEC_TEXT},
    {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
]
EXEC_SCRIPT = [*PIPELINE_STEPS, *TICKET1_STEPS, *TICKET2_STEPS]


def _full_exec(app, client, headers, script: list):
    """新团队项目跑完「澄清 → 规格 → 拆单 → 确认 → 串行执行全部完成」，返回 (项目, 伪模型)。"""
    model = use_fake_model(app, script)
    project = _create_project(client, headers, mode="team")
    _stream_messages(client, headers, project["id"], "做一个番茄钟")
    _confirm_consensus(client, headers, project["id"])
    _confirm_spec(client, headers, project["id"])
    assert _confirm_tickets(client, headers, project["id"])[-1]["type"] == "done"
    return project, model


def _rollback(client, headers, project_id, snapshot_id) -> dict:
    resp = client.post(
        f"/api/projects/{project_id}/snapshots/{snapshot_id}/rollback", headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _snapshot_by_rev(client, headers, project_id, rev: int) -> dict:
    snaps = client.get(f"/api/projects/{project_id}/snapshots", headers=headers).json()
    return next(s for s in snaps if s["rev"] == rev)


def _ticket_states(client, headers, project_id) -> tuple[list[str], list]:
    tickets = client.get(f"/api/projects/{project_id}/tickets", headers=headers).json()
    return [t["status"] for t in tickets], [t["snapshot_rev"] for t in tickets]


class TestCheckpointRollbackReset:
    def test_rollback_to_checkpoint_restores_files_and_resets_following_tickets(
        self, app, settings, client, auth_headers
    ):
        """回滚到检查点 1：文件回到骨架状态，其后的工单 2 重置为未完成并清除检查点引用。"""
        project, _ = _full_exec(app, client, auth_headers, list(EXEC_SCRIPT))
        pid = project["id"]
        pdir = _project_dir(settings, pid)
        assert (pdir / "index.html").exists() and (pdir / "timer.js").exists()

        checkpoint1 = _snapshot_by_rev(client, auth_headers, pid, 1)
        _rollback(client, auth_headers, pid, checkpoint1["id"])

        # 磁盘与文件索引都回到检查点 1 状态
        assert (pdir / "index.html").exists()
        assert not (pdir / "timer.js").exists()
        files = client.get(f"/api/projects/{pid}/files", headers=auth_headers).json()
        assert [f["path"] for f in files] == ["index.html"]

        # 检查点之后的工单重置为未完成；检查点自身对应的工单保持完成
        statuses, revs = _ticket_states(client, auth_headers, pid)
        assert statuses == ["done", "open"]
        assert revs == [1, None]

    def test_snapshot_list_marks_checkpoint_tickets(self, app, client, auth_headers):
        """快照列表标注检查点来源工单（回滚入口据此识别检查点），普通版本为 None。"""
        project, _ = _full_exec(app, client, auth_headers, list(EXEC_SCRIPT))
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        by_rev = {s["rev"]: s for s in snaps}
        assert by_rev[1]["ticket_seq"] == 1
        assert by_rev[2]["ticket_seq"] == 2

    def test_rollback_to_latest_checkpoint_resets_nothing(self, app, client, auth_headers):
        """回滚到最新检查点（即当前状态）：不重置任何工单，续跑入口仍拒绝。"""
        project, _ = _full_exec(app, client, auth_headers, list(EXEC_SCRIPT))
        pid = project["id"]
        checkpoint2 = _snapshot_by_rev(client, auth_headers, pid, 2)
        _rollback(client, auth_headers, pid, checkpoint2["id"])

        statuses, revs = _ticket_states(client, auth_headers, pid)
        assert statuses == ["done", "done"]
        assert revs == [1, 2]
        resp = client.post(f"/api/projects/{pid}/tickets/resume", json={}, headers=auth_headers)
        assert resp.status_code == 409


class TestResumeAfterRollback:
    def test_resume_starts_from_first_unfinished_and_forms_new_checkpoint(
        self, app, settings, client, auth_headers
    ):
        """回滚后续跑：从第一个未完成工单（工单 2）起点执行，工单 1 不重跑，形成新检查点。"""
        project, model = _full_exec(app, client, auth_headers, [*EXEC_SCRIPT, *TICKET2_STEPS])
        pid = project["id"]

        checkpoint1 = _snapshot_by_rev(client, auth_headers, pid, 1)
        _rollback(client, auth_headers, pid, checkpoint1["id"])

        calls_before = len(model.received_messages)
        events = _resume_tickets(client, auth_headers, pid)
        assert events[-1]["type"] == "done"
        progress = [e for e in events if e["type"] == "ticket_progress"]
        assert [(p["seq"], p["status"]) for p in progress] == [(2, "running"), (2, "done")]
        assert progress[-1]["snapshot_rev"] == 3  # 续跑生成新检查点，版本号续增

        # 续跑只执行了工单 2（写文件 + 收尾两次模型调用），指令里没有工单 1 的执行
        resume_calls = model.received_messages[calls_before:]
        assert len(resume_calls) == 2
        assert any("现在执行工单 2" in getattr(m, "content", "") for m in resume_calls[0])
        assert not any(
            "现在执行工单 1" in getattr(m, "content", "") for call in resume_calls for m in call
        )

        statuses, revs = _ticket_states(client, auth_headers, pid)
        assert statuses == ["done", "done"]
        assert revs == [1, 3]
        assert (_project_dir(settings, pid) / "timer.js").exists()

    def test_resume_only_available_with_unfinished_tickets(self, app, client, auth_headers):
        """「继续执行」仅在存在未完成工单时可用：全完成 409，回滚重置后放行。"""
        project, _ = _full_exec(app, client, auth_headers, [*EXEC_SCRIPT, *TICKET2_STEPS])
        pid = project["id"]
        resp = client.post(f"/api/projects/{pid}/tickets/resume", json={}, headers=auth_headers)
        assert resp.status_code == 409

        checkpoint1 = _snapshot_by_rev(client, auth_headers, pid, 1)
        _rollback(client, auth_headers, pid, checkpoint1["id"])
        events = _resume_tickets(client, auth_headers, pid)
        assert events[-1]["type"] == "done"

    def test_message_after_rollback_guided_to_resume(self, app, client, auth_headers):
        """回滚后回到执行期：普通消息引导去「继续执行」，不调模型不计名额。"""
        project, model = _full_exec(app, client, auth_headers, list(EXEC_SCRIPT))
        pid = project["id"]
        checkpoint1 = _snapshot_by_rev(client, auth_headers, pid, 1)
        _rollback(client, auth_headers, pid, checkpoint1["id"])

        calls_before = len(model.received_messages)
        events = _stream_messages(client, auth_headers, pid, "帮我改改")
        assert events[-1]["type"] == "done"
        text = "".join(e.get("content", "") for e in events if e["type"] == "text")
        assert "继续执行" in text
        assert len(model.received_messages) == calls_before  # 引导不调模型


class TestRepeatedRollbackResume:
    def test_rollback_resume_repeatable_with_consistent_state(self, app, settings, client, auth_headers):
        """回滚—续跑反复两轮：每轮结束后工单状态、检查点引用与磁盘文件始终一致。"""
        project, _ = _full_exec(
            app, client, auth_headers, [*EXEC_SCRIPT, *TICKET2_STEPS, *TICKET2_STEPS]
        )
        pid = project["id"]
        pdir = _project_dir(settings, pid)

        for expected_rev in (3, 4):
            checkpoint1 = _snapshot_by_rev(client, auth_headers, pid, 1)
            _rollback(client, auth_headers, pid, checkpoint1["id"])
            statuses, revs = _ticket_states(client, auth_headers, pid)
            assert statuses == ["done", "open"]
            assert revs == [1, None]
            assert not (pdir / "timer.js").exists()

            assert _resume_tickets(client, auth_headers, pid)[-1]["type"] == "done"
            statuses, revs = _ticket_states(client, auth_headers, pid)
            assert statuses == ["done", "done"]
            assert revs == [1, expected_rev]
            assert (pdir / "timer.js").exists()

        # 续跑形成的新检查点取代旧引用后，旧检查点快照仍保留来源标注（工单 0019）：
        # 回滚到旧检查点时前端仍能看到重置提示，与后端重置语义一致
        snaps = client.get(f"/api/projects/{pid}/snapshots", headers=auth_headers).json()
        by_rev = {s["rev"]: s for s in snaps}
        assert by_rev[1]["ticket_seq"] == 1
        assert by_rev[2]["ticket_seq"] == 2  # 被取代的旧检查点不失标注
        assert by_rev[4]["ticket_seq"] == 2


class TestRollbackResumeQuota:
    def test_rollback_and_resume_share_single_quota(self, app, client, auth_headers):
        """回滚与续跑不占新名额：一次名额管到底，全部完成后的迭代才按次计数。"""
        clock = FakeClock()
        app.state.rate_limiter.clock = clock
        app.state.rate_limiter.per_user_hourly = 1
        project, _ = _full_exec(
            app, client, auth_headers, [*EXEC_SCRIPT, *TICKET2_STEPS, {"text": "迭代完成。"}]
        )
        pid = project["id"]

        # 回滚不占名额（不生成）；续跑不占名额（首建名额内），尽管项目已有文件
        checkpoint1 = _snapshot_by_rev(client, auth_headers, pid, 1)
        _rollback(client, auth_headers, pid, checkpoint1["id"])
        assert _resume_tickets(client, auth_headers, pid)[-1]["type"] == "done"

        # 全部完成后进入常规迭代：名额已用完，迭代消息被拒
        resp = client.post(
            f"/api/projects/{pid}/messages", json={"content": "再改改"}, headers=auth_headers
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["reason"] == "user_hourly"

        # 窗口过期后迭代放行
        clock.advance(3601)
        events = _stream_messages(client, auth_headers, pid, "再改改")
        assert events[-1]["type"] == "done"


class TestRollbackGuards:
    def test_rollback_during_running_ticket_is_409(self, app, client, auth_headers):
        """有工单正在执行时回滚被拒：避免与串行执行流竞态覆盖文件。"""
        use_fake_model(
            app,
            [*PIPELINE_STEPS, *TICKET1_STEPS, *TICKET2_FAIL_STEPS, *TICKET2_STEPS],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        assert _confirm_tickets(client, auth_headers, project["id"])[-1]["type"] == "error"
        pid = project["id"]

        # 把失败单改成执行中（模拟断流遗留），此时回滚必须拒绝
        from app.models import Ticket as TicketRow

        with app.state.session_factory() as session:
            row = session.query(TicketRow).filter_by(project_id=pid, seq=2).one()
            row.status = "running"
            session.commit()
        checkpoint1 = _snapshot_by_rev(client, auth_headers, pid, 1)
        resp = client.post(
            f"/api/projects/{pid}/snapshots/{checkpoint1['id']}/rollback", headers=auth_headers
        )
        assert resp.status_code == 409

        # 恢复 failed 后回滚放行，续跑仍可
        with app.state.session_factory() as session:
            row = session.query(TicketRow).filter_by(project_id=pid, seq=2).one()
            row.status = "failed"
            session.commit()
        _rollback(client, auth_headers, pid, checkpoint1["id"])
        assert _resume_tickets(client, auth_headers, pid)[-1]["type"] == "done"
