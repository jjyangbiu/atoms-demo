"""自洽性核验测试（工单 0025 / ADR 0005「第 8 层」）。

接缝沿用规格 0021 既定：fake 模型 + HTTP API 集成（走 _engineer_stream）。
轮末比对模型声明的改动与磁盘真实改动（工单 0022 的指纹 diff），三种失配：
- verbal_completion：声明有改动而磁盘零改动（含自报 modify_code 而磁盘零改动）
- undeclared_change：声明零改动而磁盘有改动
- file_set_mismatch：声明文件集与磁盘真实文件集不符（两侧均非空）

失配时把「你声明的 vs 磁盘实际的」精确差异回喂一次；回喂后修正成功按自洽收尾，
仍失配则结论以结构化字段标注在卡片与收尾事件上。模型未走唯一出口时按 ADR 0005
降级链收尾：正文 JSON 恢复 → 回喂一次出口提示 → 首段散文作 summary 的兜底卡片。

所有失配断言只依赖结构化字段与文件路径事实，不依赖任何中文措辞（验收项 12）。
任何测试不得调用真实 MiniMax API。
"""

import json

from conftest import (
    EDIT_STEPS,
    FIRST_BUILD_CLARIFY_STEP,
    INTENT_MODIFY_STEP,
    JUDGE_IN_SCOPE_STEP,
    _turn_result_step,
    seed_project_files,
    use_fake_model,
    use_fake_utility_model,
)
from test_changed_set import _load_log
from test_generation import _stream_messages
from test_projects import _create_project
from test_team_exec import _confirm_tickets
from test_team_tickets import SPEC_TEXT, TICKETS_PAYLOAD, _confirm_consensus, _confirm_spec


class TestMismatchKinds:
    """三种失配：判定为结构化结论，回喂恰好一次，仍失配则标注收尾。"""

    def test_verbal_completion_mismatch_refed_once_then_flagged(
        self, app, client, auth_headers
    ):
        """声明有改动而磁盘零改动 → verbal_completion；精确差异回喂一次。"""
        summary = "把首页标题换成了深色主题。"
        model = use_fake_model(
            app,
            [
                _turn_result_step(summary=summary, changed_files=["index.html"]),
                # 回喂后仍原样申报 → 失配标注收尾
                _turn_result_step(summary=summary, changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "把标题改成深色主题")

        # 回喂恰好一次：第二次模型调用 = 第一次的消息 + 出口调用 + 一条工具结果
        assert len(model.received_messages) == 2
        first, second = model.received_messages
        assert len(second) == len(first) + 2
        feedback = second[-1]
        assert type(feedback).__name__ == "ToolMessage"
        # 回喂内容携带「声明的 vs 磁盘实际的」精确差异事实：声明文件路径在其中
        assert "index.html" in feedback.content

        # 卡片：核验结论是结构化字段；summary 原样、不再被追加文案污染
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert messages[-1]["kind"] == "turn_result"
        card = json.loads(messages[-1]["content"])
        assert card["summary"] == summary
        assert card["consistency"] == "mismatch"
        assert card["mismatch_kind"] == "verbal_completion"
        assert card["changed_files"] == []  # 磁盘为权威
        assert card["declared_files"] == ["index.html"]  # 申报原样留档

        # 收尾事件：移除 warning/no_change 字段，携带核验结论与产物摘要；仍是最后一个事件
        done = events[-1]
        assert done["type"] == "done"
        assert "warning" not in done and "no_change" not in done
        assert done["verdict"] == "mismatch"
        assert done["mismatch_kind"] == "verbal_completion"
        assert done["artifact"] == {
            "intent": "modify_code",
            "summary": summary,
            "changed_files": [],
        }

        # 零改动轮仍不留档快照（ADR 0004 硬闸不变）
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == [] and card["snapshot_id"] is None
        # 核验结论只经结构化字段呈现：正文不再追加系统核验说明文案
        assert not any(m["role"] == "engineer" and m["kind"] == "text" for m in messages)

    def test_undeclared_change_mismatch_flagged(self, app, client, auth_headers):
        """声明零改动（自报 no_change）而磁盘有改动 → undeclared_change。"""
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(
                    intent="no_change",
                    summary="核对后无需改动。",
                    no_change_reason="目标状态已存在。",
                ),
                _turn_result_step(
                    intent="no_change",
                    summary="核对后无需改动。",
                    no_change_reason="目标状态已存在。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "确认下标题")

        # 回喂恰好一次（read、edit、首次出口、二次出口）
        assert len(model.received_messages) == 4
        feedback = model.received_messages[-1][len(model.received_messages[-2]) + 1]
        assert type(feedback).__name__ == "ToolMessage"
        assert "index.html" in feedback.content  # 磁盘真实改动出现在差异回喂里

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "mismatch"
        assert card["mismatch_kind"] == "undeclared_change"
        assert card["changed_files"] == ["index.html"]  # 磁盘为权威
        assert card["declared_files"] == []

        done = events[-1]
        assert done["verdict"] == "mismatch" and done["mismatch_kind"] == "undeclared_change"
        assert "warning" not in done and "no_change" not in done
        # 有真实改动的轮次快照照建（失配标注不没收用户的工作成果）
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1 and card["snapshot_rev"] == snaps[0]["rev"]

    def test_no_change_intent_overrides_declared_files(self, app, client, auth_headers):
        """自报 no_change 却在 changed_files 列出与磁盘一致的文件 → 意图即零改动申报，仍判 undeclared_change。"""
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(
                    intent="no_change",
                    summary="核对后无需改动。",
                    changed_files=["index.html"],
                    no_change_reason="目标状态已存在。",
                ),
                _turn_result_step(
                    intent="no_change",
                    summary="核对后无需改动。",
                    changed_files=["index.html"],
                    no_change_reason="目标状态已存在。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "确认下标题")

        # 矛盾申报同样只回喂一次（read、edit、首次出口、二次出口）
        assert len(model.received_messages) == 4

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "mismatch"
        assert card["mismatch_kind"] == "undeclared_change"
        assert card["changed_files"] == ["index.html"]  # 磁盘为权威
        assert card["declared_files"] == ["index.html"]  # 申报原样留档

        done = events[-1]
        assert done["verdict"] == "mismatch" and done["mismatch_kind"] == "undeclared_change"

    def test_file_set_mismatch_flagged(self, app, client, auth_headers):
        """声明文件集与磁盘真实文件集不符（两侧均非空）→ file_set_mismatch。"""
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(changed_files=["index.html", "styles.css"]),
                _turn_result_step(changed_files=["index.html", "styles.css"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        assert len(model.received_messages) == 4
        feedback = model.received_messages[-1][len(model.received_messages[-2]) + 1]
        # 精确差异：多报的与真实的都在回喂里
        assert "styles.css" in feedback.content and "index.html" in feedback.content

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "mismatch"
        assert card["mismatch_kind"] == "file_set_mismatch"
        assert card["changed_files"] == ["index.html"]
        assert card["declared_files"] == ["index.html", "styles.css"]
        assert events[-1]["verdict"] == "mismatch"
        assert events[-1]["mismatch_kind"] == "file_set_mismatch"


class TestRefeedOutcomes:
    """回喂一次的两条出路：修正成功按自洽收尾；一致申报不触发任何回喂。"""

    def test_corrected_declaration_after_refeed_is_consistent(
        self, app, client, auth_headers
    ):
        """失配 → 精确差异回喂 → 修正申报（含路径归一化）→ 自洽收尾。"""
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(changed_files=["styles.css"]),
                _turn_result_step(summary="标题已换成深色主题。", changed_files=["./index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        assert len(model.received_messages) == 4
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "consistent"
        assert card["mismatch_kind"] is None
        # 申报原样留档（归一化只用于比对，不改写申报）
        assert card["declared_files"] == ["./index.html"]
        assert card["changed_files"] == ["index.html"]
        done = events[-1]
        assert done["verdict"] == "consistent" and "mismatch_kind" not in done
        assert done["artifact"] == {
            "intent": "modify_code",
            "summary": "标题已换成深色主题。",
            "changed_files": ["index.html"],
        }
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1

    def test_consistent_declaration_never_refed(self, app, client, auth_headers):
        """申报与磁盘一致：不触发回喂，出口一次通过。"""
        model = use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        # read、edit、出口：共 3 次模型调用，无额外回喂
        assert len(model.received_messages) == 3
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "consistent" and card["mismatch_kind"] is None
        assert events[-1]["verdict"] == "consistent"

    def test_legal_no_change_exit_is_consistent(self, app, client, auth_headers):
        """自报 no_change 且磁盘零改动：自洽的合法结局，零惩罚、无快照。"""
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                _turn_result_step(
                    intent="no_change",
                    summary="现状已符合要求。",
                    no_change_reason="目标状态已存在于磁盘。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "确认下标题")

        assert len(model.received_messages) == 2
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "consistent" and card["mismatch_kind"] is None
        assert card["changed_files"] == []
        done = events[-1]
        assert done["verdict"] == "consistent"
        assert "warning" not in done and "no_change" not in done
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []


class TestDegradationChain:
    """ADR 0005 降级链：正文 JSON 恢复 → 回喂一次唯一出口 → 散文作 summary 兜底。"""

    def test_prose_refed_once_then_fallback_card(self, app, client, auth_headers):
        """两轮散文都不走出口：回喂一次后兜底为卡片，首段散文作 summary。"""
        model = use_fake_model(
            app, [{"text": "好的，处理好了。"}, {"text": "我就不调工具。"}]
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        # 回喂恰好一次，点名唯一出口（工具名是接口契约，非措辞断言）
        assert len(model.received_messages) == 2
        first, second = model.received_messages
        assert len(second) == len(first) + 2
        feedback = second[-1]
        assert type(feedback).__name__ == "HumanMessage"
        assert "submit_turn_result" in feedback.content

        # 兜底卡片：声明一律以磁盘真实值填充（零改动 → no_change + 系统理由）
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert messages[-1]["kind"] == "turn_result"
        assert not any(m["role"] == "engineer" and m["kind"] == "text" for m in messages)
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "fallback" and card["mismatch_kind"] is None
        assert card["summary"] == "好的，处理好了。"  # 首段散文作 summary
        assert card["intent"] == "no_change"  # 意图按磁盘事实判定
        assert card["no_change_reason"]
        assert card["changed_files"] == [] and card["declared_files"] == []

        done = events[-1]
        assert done["type"] == "done" and done["verdict"] == "fallback"
        assert "warning" not in done and "no_change" not in done
        assert done["artifact"] == {
            "intent": "no_change",
            "summary": "好的，处理好了。",
            "changed_files": [],
        }
        # 零改动轮仍不留档（硬闸不变）
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == [] and card["snapshot_id"] is None

    def test_fallback_card_keeps_disk_truth_and_snapshot(self, app, client, auth_headers):
        """磁盘有真实改动时兜底：卡片意图/清单取磁盘事实，快照与迭代日志照常。"""
        model = use_fake_model(
            app, [*EDIT_STEPS, {"text": "标题现在是 v2。"}, {"text": "不调工具。"}]
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "把 v1 改成 v2")

        assert len(model.received_messages) == 4  # read、edit、散文、散文
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "fallback"
        assert card["intent"] == "modify_code"
        assert card["summary"] == "标题现在是 v2。"
        assert card["changed_files"] == ["index.html"] and card["declared_files"] == []
        assert events[-1]["verdict"] == "fallback"
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1 and card["snapshot_rev"] == snaps[0]["rev"]
        log = _load_log(app, project["id"])
        assert log[-1]["files"] == ["index.html"]

    def test_body_json_recovered_consistent(self, app, client, auth_headers):
        """产物 JSON 写进正文且申报与磁盘一致：恢复为自洽卡片，无回喂。"""
        payload = json.dumps(
            {
                "intent": "modify_code",
                "summary": "标题现为深色主题。",
                "changed_files": ["index.html"],
                "no_change_reason": "",
            },
            ensure_ascii=False,
        )
        model = use_fake_model(app, [*EDIT_STEPS, {"text": f"本轮产物如下：\n{payload}"}])
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        assert len(model.received_messages) == 3  # 恢复成功即收束，无回喂
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "consistent"
        assert card["summary"] == "标题现为深色主题。"
        assert card["changed_files"] == ["index.html"]
        assert events[-1]["verdict"] == "consistent"

    def test_body_json_recovered_mismatch_refed_then_corrected(
        self, app, client, auth_headers
    ):
        """正文 JSON 恢复出的申报失配：同样回喂一次精确差异，修正后自洽收尾。"""
        payload = json.dumps(
            {
                "intent": "modify_code",
                "summary": "标题现为深色主题。",
                "changed_files": ["styles.css"],
                "no_change_reason": "",
            },
            ensure_ascii=False,
        )
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                {"text": f"本轮产物如下：\n{payload}"},
                _turn_result_step(summary="标题现为深色主题。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        assert len(model.received_messages) == 4
        feedback = model.received_messages[-1][len(model.received_messages[-2]) + 1]
        assert type(feedback).__name__ == "HumanMessage"
        # 精确差异：申报的与磁盘真实的都在回喂里
        assert "styles.css" in feedback.content and "index.html" in feedback.content
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "consistent" and card["mismatch_kind"] is None
        assert card["declared_files"] == ["index.html"]
        assert events[-1]["verdict"] == "consistent"


class TestCoverageScope:
    """覆盖面：团队工单执行同受核验，但不入迭代日志；系统提示不再含口头完成条款。"""

    def test_team_ticket_execution_covered_but_not_logged(
        self, app, client, auth_headers
    ):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                {"tool_calls": [("submit_tickets", {"tickets": TICKETS_PAYLOAD})]},
                {
                    "tool_calls": [
                        ("write_file", {"path": "index.html", "content": "<h1>骨架</h1>"})
                    ]
                },
                # 工单 1：失配申报 → 回喂 → 修正
                _turn_result_step(summary="骨架页面就绪。", changed_files=["ghost.css"]),
                _turn_result_step(summary="骨架页面就绪。", changed_files=["index.html"]),
                {
                    "tool_calls": [
                        ("write_file", {"path": "timer.js", "content": "// 计时核心"})
                    ]
                },
                _turn_result_step(summary="计时核心就绪。", changed_files=["timer.js"]),
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        progress = [
            (p["seq"], p["status"]) for p in events if p["type"] == "ticket_progress"
        ]
        assert progress == [(1, "running"), (1, "done"), (2, "running"), (2, "done")]
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        cards = [json.loads(m["content"]) for m in messages if m["kind"] == "turn_result"]
        assert len(cards) == 2
        # 工单执行同受自洽性核验：回喂修正后按自洽收尾
        assert cards[0]["consistency"] == "consistent"
        assert cards[0]["changed_files"] == ["index.html"]
        assert cards[1]["consistency"] == "consistent"
        # 但不入迭代日志（record_iteration=False 语义不变）
        assert _load_log(app, project["id"]) == []

    def test_system_prompt_drops_verbal_completion_clause(self, app, client, auth_headers):
        """系统提示：「严禁口头完成」整条删除；编辑纪律（压缩后）保留。"""
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                _turn_result_step(intent="no_change", summary="无改动。", no_change_reason="仅确认。"),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "确认下")

        system = model.received_messages[0][0]
        assert type(system).__name__ == "SystemMessage"
        content = system.content
        assert "口头完成" not in content
        # 保留（压缩后）的编辑纪律：只能用编辑工具改文件、替换文本须唯一、只动受影响区域
        assert "edit_file" in content and "read_file" in content
        assert "old_text" in content and "唯一" in content


class TestReworkOnUnfinishedModify:
    """未完成自动返工（用户诉求「未完成 → 返工」）：分类器确信本轮意在改动，模型却
    用文字声称改完而磁盘零改动——先回喂一次逼它真正动文件，动了再按自洽收尾。

    仅在分类器确信 modify_code 时生效（散文兜底路径不经自洽性核验，是「口头声称改完
    但没改」唯一绕过实质回喂的出口，此处补一次实质返工闸）；分类降级（None）不触发
    本闸，仍走既有散文降级链兜底成诚实卡片（见 TestDegradationChain）。返工与自洽性
    回喂各自独立计一次预算。
    """

    def test_prose_claim_zero_change_reworked_then_real_edit(
        self, app, client, auth_headers
    ):
        """散文声称完成、磁盘零改动 → 返工回喂一次 → 模型真正改盘 → 自洽收尾、留快照。"""
        utility = use_fake_utility_model(app, [INTENT_MODIFY_STEP, JUDGE_IN_SCOPE_STEP])
        model = use_fake_model(
            app,
            [
                {"text": "我已经把标题改成深色主题了。"},  # 散文声称完成，磁盘零改动
                *EDIT_STEPS,  # 返工回喂后真正动文件（v1 → v2）
                _turn_result_step(summary="标题已换成深色主题。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "把标题改成深色主题")

        # 返工回喂恰好一次：散文步之后追加一条 HumanMessage 逼真改盘（点名写文件工具）
        assert len(model.received_messages) == 4  # 散文、read、edit、出口
        feedback = model.received_messages[1][-1]
        assert type(feedback).__name__ == "HumanMessage"
        assert "write_file" in feedback.content and "edit_file" in feedback.content

        # 真改盘且按自洽收尾（不是散文兜底的 fallback，也不是失配）
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "consistent" and card["mismatch_kind"] is None
        assert card["intent"] == "modify_code"
        assert card["changed_files"] == ["index.html"]
        assert events[-1]["verdict"] == "consistent"
        # 分类一次 + 裁判一次（真改动触发裁判）
        assert len(utility.received_messages) == 2
        # 真实改动轮留快照
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1

    def test_rework_budget_once_then_falls_to_prose_fallback(
        self, app, client, auth_headers
    ):
        """返工预算轮级一次：回喂后仍散文不动手 → 落回既有散文降级链兜底成诚实卡片。

        返工回喂（1 次）与散文出口回喂（1 次）各自独立：三段散文分别触发返工回喂、
        散文出口回喂，第三段放行兜底。兜底卡片按磁盘事实为 no_change，绝不谎报完成。
        """
        utility = use_fake_utility_model(app, [INTENT_MODIFY_STEP, JUDGE_IN_SCOPE_STEP])
        model = use_fake_model(
            app,
            [
                {"text": "我已经改好了。"},  # 返工回喂（磁盘零改动）
                {"text": "确实改好了，真的。"},  # 仍零改动 → 散文出口回喂
                {"text": "我就是不调工具。"},  # 仍不听 → 放行兜底
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "把标题改成深色主题")

        # 两次回喂（返工 + 散文出口）后第三段放行：共三次模型调用
        assert len(model.received_messages) == 3
        # 兜底卡片按磁盘事实诚实呈现零改动，不谎报完成
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        card = json.loads(messages[-1]["content"])
        assert card["consistency"] == "fallback"
        assert card["intent"] == "no_change"
        assert card["changed_files"] == []
        assert events[-1]["verdict"] == "fallback"
        # 分类 modify_code 的零改动兜底轮属可疑零改动（合成 no_change 申报），仍进裁判
        # （工单 0030）：分类一次 + 裁判一次
        assert len(utility.received_messages) == 2
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []
