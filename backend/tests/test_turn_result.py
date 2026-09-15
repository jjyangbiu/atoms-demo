"""轮次产物终结出口测试（工单 0024 / ADR 0005「第 8 层」）。

两个接缝（沿用 0022 既定接缝）：
- 解析器单测：parse_turn_result_payload / recover_turn_result_payload
  （app.agent.tools 的公共函数，同 parse_clarify_payload 房规）；
- fake 模型集成：HTTP API 层走 _engineer_stream，覆盖正常出口、
  正文 JSON 恢复、散文兜底三条路径。
任何测试不得调用真实 MiniMax API。
"""

import json

import pytest
from conftest import (
    FIRST_BUILD_CLARIFY_STEP,
    confirm_first_build,
    seed_project_files,
    use_fake_model,
)
from test_changed_set import _load_log
from test_generation import _stream_messages
from test_projects import _create_project
from test_team_exec import _confirm_tickets
from test_team_tickets import SPEC_TEXT, TICKETS_PAYLOAD, _confirm_consensus, _confirm_spec


def _turn_result_step(
    intent: str = "modify_code",
    summary: str = "已把标题改为深色主题。",
    changed_files: list[str] | None = None,
    no_change_reason: str = "",
) -> dict:
    """伪模型脚本步：调用 submit_turn_result 提交轮次产物（共享脚本步房规）。"""
    payload = {
        "intent": intent,
        "summary": summary,
        "changed_files": changed_files if changed_files is not None else [],
        "no_change_reason": no_change_reason,
    }
    return {
        "tool_calls": [
            ("submit_turn_result", {"payload": json.dumps(payload, ensure_ascii=False)})
        ]
    }


EDIT_STEPS = [
    {"tool_calls": [("read_file", {"path": "index.html"})]},
    {
        "tool_calls": [
            ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
        ]
    },
]
"""伪模型脚本步：一次真实成功的文件编辑（磁盘产生改动）。"""


class TestTurnResultExit:
    """正常出口：工具调用即终结信号，轮次产物落库为结构化卡片。"""

    def test_normal_exit_persists_card_with_disk_authoritative_files(
        self, app, settings, client, auth_headers
    ):
        use_fake_model(
            app,
            [
                *EDIT_STEPS,
                # 谎报改动清单：卡片必须以磁盘真实改动为权威（0022 成果）
                _turn_result_step(changed_files=["styles.css", "phantom.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        # SSE：turn_result 事件外发、done 收尾；出口工具事件不外发（对用户不可见）
        tr_events = [e for e in events if e["type"] == "turn_result"]
        assert tr_events and events[-1]["type"] == "done"
        assert all(
            e.get("name") != "submit_turn_result" for e in events if e["type"] == "tool"
        )

        # 落库：新结构化消息类型携完整 payload
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert messages[-1]["kind"] == "turn_result"
        card = json.loads(messages[-1]["content"])
        assert card["intent"] == "modify_code"
        assert card["summary"] == "已把标题改为深色主题。"
        # 改动清单以磁盘为权威：谎报的 styles.css/phantom.html 不得出现
        assert card["changed_files"] == ["index.html"]
        # 完整 payload：模型申报原样留档（0025 自洽性核验的原料），但不作权威
        assert card["declared_files"] == ["styles.css", "phantom.html"]
        # 卡片携本轮快照引用（展开看本轮 diff 的入口）
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1
        assert card["snapshot_id"] == snaps[0]["id"]
        assert card["snapshot_rev"] == snaps[0]["rev"]

        # 正常出口轮：done 不携任何惩罚性字段；迭代日志照常记磁盘真实改动
        assert "warning" not in events[-1] and "no_change" not in events[-1]
        log = _load_log(app, project["id"])
        assert log[-1]["files"] == ["index.html"]

        # SSE 上的卡片与落库内容一致（前端流结束后以历史重渲染）
        assert json.loads(tr_events[0]["content"]) == card

    def test_no_change_exit_is_legal_with_no_penalty(
        self, app, settings, client, auth_headers
    ):
        """自报「无需改动」合法收尾：卡片照出、无快照、done 不携任何惩罚性字段。"""
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                _turn_result_step(
                    intent="no_change",
                    summary="现状已符合要求，未做改动。",
                    no_change_reason="目标状态已存在于磁盘，read_file 核实无误。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "确认下标题")

        tr_events = [e for e in events if e["type"] == "turn_result"]
        assert tr_events and events[-1]["type"] == "done"
        # 合法结局不触发惩罚：无 warning / no_change 字段，正文无系统核验标注
        assert "warning" not in events[-1] and "no_change" not in events[-1]
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert messages[-1]["kind"] == "turn_result"
        card = json.loads(messages[-1]["content"])
        assert card["intent"] == "no_change"
        assert card["changed_files"] == []
        assert card["no_change_reason"]
        # 零改动轮不留档（硬闸不变）；卡片无可展开的 diff
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []
        assert card["snapshot_id"] is None and card["snapshot_rev"] is None
        # 出口收尾不再另落一条工程师散文消息（总结只在卡片里）
        assert not any(
            m["role"] == "engineer" and m["kind"] == "text" for m in messages
        )

    def test_card_summary_reaches_next_round_context(self, app, client, auth_headers):
        """轮次产物以结论摘要入后续轮上下文；卡片原始 JSON 不入上下文。"""
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(summary="标题已换成深色主题。", changed_files=["index.html"]),
                {"text": "好的。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "改一下")
        _stream_messages(client, auth_headers, project["id"], "再确认一下现状")

        call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in call]
        assert any("标题已换成深色主题。" in c and "轮次产物" in c for c in contents)
        assert not any("declared_files" in c for c in contents)

    def test_invalid_payload_sent_back_to_model(self, app, client, auth_headers):
        """产物非法（枚举越界）时错误交还模型自行修正，不中断循环、不外发坏卡片。"""
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                {
                    "tool_calls": [
                        ("submit_turn_result", {"payload": "这显然不是 JSON"})
                    ]
                },
                _turn_result_step(changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        assert any(e["type"] == "turn_result" for e in events)
        assert events[-1]["type"] == "done"
        tool_replies = [
            m
            for call in model.received_messages
            for m in call
            if type(m).__name__ == "ToolMessage"
        ]
        assert any("不合法" in m.content for m in tool_replies)
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        cards = [m for m in messages if m["kind"] == "turn_result"]
        assert len(cards) == 1
        # 证据链（ADR 0005：工具事件仍然落库）：非法出口尝试虽不外发，
        # 仍按 _persist_event 既有契约留档，刷新页面可回看完整过程
        assert any(
            m["kind"] == "event"
            and "submit_turn_result" in m["content"]
            and '"status": "error"' in m["content"]
            for m in messages
        )

    def test_false_modify_code_claim_keeps_system_verification(
        self, app, settings, client, auth_headers
    ):
        """自报「已改动代码」但磁盘零改动：卡片路径不得比散文更干净——
        H1 硬输出闸同等生效（系统无条件追加核验标注、done 携警告）；
        完整的声明与事实成对核验归工单 0025。"""
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                _turn_result_step(
                    intent="modify_code",
                    summary="已完成修改。",
                    changed_files=["index.html"],
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        done = events[-1]
        assert done["type"] == "done"
        # 与散文路径同一警告（口头完成不因改走出口而免罚）
        assert done.get("warning") == "本轮未产生任何文件改动"
        assert done.get("no_change") is True
        tr_events = [e for e in events if e["type"] == "turn_result"]
        card = json.loads(tr_events[0]["content"])
        assert card["intent"] == "modify_code" and card["changed_files"] == []
        # 核验标注由系统撰写、由代码保证：声称与核验成对出现，裸谎言无法单独存活
        assert "系统核验" in card["summary"] and "不符" in card["summary"]
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        persisted = json.loads(
            [m for m in messages if m["kind"] == "turn_result"][-1]["content"]
        )
        assert persisted["summary"] == card["summary"]
        # 零改动轮不留档（硬闸不变）
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == [] and card["snapshot_id"] is None


class TestTurnResultRecovery:
    """模型把产物 JSON 写进正文而没走工具：按房规恢复为卡片路径，
    不得把 JSON 残段渲染成裸文本气泡；恢复失败才落回散文兜底。"""

    def test_json_in_prose_recovered_as_card(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                *EDIT_STEPS,
                {
                    "text": "本轮产物如下：\n"
                    + json.dumps(
                        {
                            "intent": "modify_code",
                            "summary": "标题现为深色主题。",
                            "changed_files": ["index.html"],
                            "no_change_reason": "",
                        },
                        ensure_ascii=False,
                    )
                },
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        assert any(e["type"] == "turn_result" for e in events)
        assert events[-1]["type"] == "done"
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        # 落库为卡片而非裸 JSON 文本（前端流结束后以历史重渲染）
        assert messages[-1]["kind"] == "turn_result"
        card = json.loads(messages[-1]["content"])
        assert card["summary"] == "标题现为深色主题。"
        assert card["changed_files"] == ["index.html"]
        assert not any(
            m["role"] == "engineer" and m["kind"] == "text" for m in messages
        )
        # 恢复路径同样正常留档
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1 and card["snapshot_rev"] == snaps[0]["rev"]

    def test_prose_fallback_keeps_existing_behavior(self, app, settings, client, auth_headers):
        """散文兜底（expand 只加不删）：既有收尾行为原样——text 落库、
        快照照建、迭代日志的改动文件以磁盘真实值填充，绝无 turn_result 卡片。"""
        use_fake_model(app, [*EDIT_STEPS, {"text": "已更新。"}])
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _stream_messages(client, auth_headers, project["id"], "改一下")

        assert all(e["type"] != "turn_result" for e in events)
        assert events[-1]["type"] == "done" and events[-1]["text"] == "已更新。"
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert not any(m["kind"] == "turn_result" for m in messages)
        assert messages[-1]["role"] == "engineer" and messages[-1]["kind"] == "text"
        assert messages[-1]["content"] == "已更新。"
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1
        # 兜底路径下声明改动一律以磁盘真实值填充（迭代日志）
        log = _load_log(app, project["id"])
        assert log[-1]["files"] == ["index.html"]


class TestParseTurnResultPayload:
    """解析器单测：schema 校验层（JSON 合法性之外的字段/类型/枚举校验）。"""

    def test_valid_modify_code_payload(self):
        from app.agent.tools import parse_turn_result_payload

        raw = json.dumps(
            {
                "intent": "modify_code",
                "summary": "已把标题改为深色主题。",
                "changed_files": ["index.html", "styles.css"],
                "no_change_reason": "",
            },
            ensure_ascii=False,
        )
        parsed, reason = parse_turn_result_payload(raw)
        assert reason == ""
        assert parsed == {
            "intent": "modify_code",
            "summary": "已把标题改为深色主题。",
            "changed_files": ["index.html", "styles.css"],
            "no_change_reason": "",
        }

    def test_valid_no_change_payload(self):
        from app.agent.tools import parse_turn_result_payload

        raw = json.dumps(
            {
                "intent": "no_change",
                "summary": "现状已符合要求。",
                "changed_files": [],
                "no_change_reason": "目标状态已存在于磁盘。",
            },
            ensure_ascii=False,
        )
        parsed, reason = parse_turn_result_payload(raw)
        assert reason == "" and parsed["intent"] == "no_change"

    @pytest.mark.parametrize(
        "bad",
        [
            "这显然不是 JSON",  # 非 JSON
            '[{"intent": "modify_code"}]',  # 数组而非对象
            '{"intent": "rewrite_all", "summary": "x", "changed_files": []}',  # 枚举越界
            '{"intent": "", "summary": "x", "changed_files": []}',  # 缺 intent
            '{"intent": "modify_code", "summary": "", "changed_files": []}',  # 缺总结
            # 总结与理由双缺
            '{"intent": "no_change", "summary": "", "changed_files": [], "no_change_reason": ""}',
            # 缺理由（no_change 须给出无需改动的理由）
            '{"intent": "no_change", "summary": "无需改动。", "changed_files": []}',
            '{"intent": "modify_code", "summary": "x", "changed_files": "index.html"}',  # 非数组
            '{"intent": "modify_code", "summary": "x", "changed_files": [""]}',  # 空路径
        ],
    )
    def test_rejects_malformed_payload(self, bad):
        """非法产物一律拒绝并给出错误文案（交还模型修正，不中断循环）。"""
        from app.agent.tools import parse_turn_result_payload

        parsed, reason = parse_turn_result_payload(bad)
        assert parsed is None and reason


class TestRecoverTurnResultPayload:
    """正文 JSON 恢复（房规）：从后往前扫描候选起点，解析失败补右括号重试。"""

    def test_full_json_in_prose_recovered(self):
        from app.agent.tools import recover_turn_result_payload

        raw = (
            "好的，本轮产物如下：\n"
            '{"intent": "modify_code", "summary": "已更新标题。", '
            '"changed_files": ["index.html"], "no_change_reason": ""}'
        )
        parsed = recover_turn_result_payload(raw)
        assert parsed is not None
        assert parsed["intent"] == "modify_code"
        assert parsed["summary"] == "已更新标题。"

    def test_truncated_tail_recovered_with_appended_brace(self):
        """尾部被截断缺 `}`：补右括号重试后恢复。"""
        from app.agent.tools import recover_turn_result_payload

        raw = (
            '{"intent": "no_change", "summary": "无需改动。", '
            '"changed_files": [], "no_change_reason": "已是目标状态"'
        )
        parsed = recover_turn_result_payload(raw)
        assert parsed is not None and parsed["intent"] == "no_change"

    def test_scan_back_to_front_skips_inner_objects(self):
        """从后往前：先命中的内层对象过不了校验，继续前扫至外层完整对象。"""
        from app.agent.tools import recover_turn_result_payload

        raw = (
            '{"intent": "modify_code", "summary": "见说明", '
            '"changed_files": ["a.js"], "no_change_reason": "", '
            '"meta": {"nested": true}}'
        )
        parsed = recover_turn_result_payload(raw)
        assert parsed is not None and parsed["changed_files"] == ["a.js"]

    def test_free_prose_not_recovered(self):
        """自由散文过不了 intent 枚举闸，天然不会误恢复。"""
        from app.agent.tools import recover_turn_result_payload

        assert recover_turn_result_payload("已完成修改，效果很好。{}") is None
        assert recover_turn_result_payload("") is None


class TestSharedExitPath:
    """首建轮与团队工单执行走同一条工程师流路径的同一个出口，不单加开关。"""

    def test_first_build_round_exits_through_turn_result(
        self, app, settings, client, auth_headers
    ):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {
                    "tool_calls": [
                        ("write_file", {"path": "index.html", "content": "<h1>番茄钟</h1>"})
                    ]
                },
                _turn_result_step(summary="番茄钟首版已就绪。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        events = confirm_first_build(client, auth_headers, project["id"])

        assert any(e["type"] == "turn_result" for e in events)
        assert events[-1]["type"] == "done"
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert messages[-1]["kind"] == "turn_result"
        card = json.loads(messages[-1]["content"])
        assert card["changed_files"] == ["index.html"]
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1 and card["snapshot_rev"] == snaps[0]["rev"]

    def test_team_ticket_execution_exits_through_turn_result(
        self, app, settings, client, auth_headers
    ):
        """每张工单经同一出口收尾：卡片照出、检查点照建，但不入迭代日志。"""
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
        # 每张工单一张产物卡片（事件外发 + 落库）
        assert len([e for e in events if e["type"] == "turn_result"]) == 2
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        cards = [json.loads(m["content"]) for m in messages if m["kind"] == "turn_result"]
        assert [c["changed_files"] for c in cards] == [["index.html"], ["timer.js"]]
        # 工单执行是内部编排：不入迭代日志（record_iteration=False 语义不变）
        assert _load_log(app, project["id"]) == []
        # 每单一个检查点快照，卡片携对应引用
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 2
        assert [c["snapshot_rev"] for c in cards] == sorted(s["rev"] for s in snaps)
        assert [c["snapshot_id"] for c in cards] == [
            s["id"] for s in sorted(snaps, key=lambda s: s["rev"])
        ]
