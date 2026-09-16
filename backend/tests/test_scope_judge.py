"""正确性裁判测试（工单 0028 阶段一 / ADR 0005「第 8 层」正确性裁决）。

接缝沿用规格 0021 既定：
- 主接缝 HTTP API 层：三种裁决值的卡片/日志/快照处置、触发条件、降级、名额，
  全部经真实端点断言；
- 辅助接缝「辅助模型注入点」（与分类器共用 use_fake_utility_model，分类恒为
  第一次调用、裁判恒为轮末最后一次，按序排布脚本即可）：received_messages 使
  「裁判输入契约」与「裁判不得看见智能体自述」可断言。

阶段一语义：只标注不阻断——越界轮照常留档快照，卡片列出越界段落并标红，
交用户裁决；「越界轮不留档」是阶段二的一次开关翻转，不在本工单范围。
裁判失败（网络/超时/非法输出）→ 复用既有重试次数与退避语义重试，仍失败则
标注「核验未完成」（scope_verdict=unverified）且快照照建——安全网故障不得
扣住用户的劳动成果，且必须显式化而非静默吞掉。
任何测试不得调用真实 MiniMax API。
"""

import json

from conftest import (
    EDIT_STEPS,
    FIRST_BUILD_CLARIFY_STEP,
    INTENT_CONSULT_STEP,
    INTENT_MODIFY_STEP,
    JUDGE_IN_SCOPE_STEP,
    JUDGE_OUT_OF_SCOPE_STEP,
    _intent_step,
    _judge_step,
    _turn_result_step,
    confirm_first_build,
    seed_project_files,
    use_fake_model,
    use_fake_utility_model,
)
from test_generation import _stream_messages
from test_iteration_log import _load_log
from test_projects import _create_project
from test_team_tickets import FakeClock


def _joined(messages: list) -> str:
    """把一次模型调用收到的消息列表拍平成文本（输入契约断言用）。"""
    return "\n".join(str(m.content) for m in messages)


EDIT_STEPS_V2_V3 = [
    {"tool_calls": [("read_file", {"path": "index.html"})]},
    {
        "tool_calls": [
            ("edit_file", {"path": "index.html", "old_text": "v2", "new_text": "v3"})
        ]
    },
]
"""伪模型脚本步：第二轮真实编辑（接 EDIT_STEPS 之后，index.html 内容 v2 → v3）。"""


def _last_card(client, headers, project_id) -> dict:
    """读回本项目最后一张轮次产物卡片（HTTP 接缝：卡片以消息落库）。"""
    messages = client.get(
        f"/api/projects/{project_id}/messages", headers=headers
    ).json()
    cards = [json.loads(m["content"]) for m in messages if m["kind"] == "turn_result"]
    assert cards, "本轮应产出产物卡片"
    return cards[-1]


class TestVerdictDisposal:
    """三种裁决值的处置（工单 0028 验收）：越界标红列段落、未完成只标注、范围内干净收尾。"""

    def test_in_scope_round_finishes_clean(self, app, client, auth_headers):
        utility = use_fake_utility_model(app, [INTENT_MODIFY_STEP, JUDGE_IN_SCOPE_STEP])
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        # 范围内 → 干净收尾：无任何额外打扰（无越界段落清单）
        assert events[-1]["type"] == "done" and "error" not in [e["type"] for e in events]
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "in_scope"
        assert card["out_of_scope_segments"] == []
        assert events[-1]["scope_verdict"] == "in_scope"
        # 照常留档快照、裁决写入迭代日志
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1 and card["snapshot_rev"] == snaps[0]["rev"]
        log = _load_log(app, project["id"])
        assert log[-1]["scope_verdict"] == "in_scope"
        # 分类 + 裁判恰各一次，都走辅助模型注入点
        assert len(utility.received_messages) == 2

    def test_out_of_scope_round_lists_segments_and_still_snapshots(
        self, app, client, auth_headers
    ):
        """阶段一：越界只标注不阻断——卡片列出越界段落，快照照常留档。"""
        use_fake_utility_model(app, [INTENT_MODIFY_STEP, JUDGE_OUT_OF_SCOPE_STEP])
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done" and "error" not in [e["type"] for e in events]
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "out_of_scope"
        assert card["out_of_scope_segments"] == [
            {"file": "index.html", "start_line": 3, "end_line": 8, "reason": "顺带重写了页脚，用户未要求。"}
        ]
        # 阶段一语义：越界轮照常留档快照（阶段二才翻转为不留档）
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1
        log = _load_log(app, project["id"])
        assert log[-1]["scope_verdict"] == "out_of_scope"

    def test_incomplete_round_annotated_and_snapshotted(self, app, client, auth_headers):
        """未完成只标注、照常留档：改动合法、只是没做完，不该惩罚。"""
        use_fake_utility_model(app, [INTENT_MODIFY_STEP, _judge_step(verdict="incomplete")])
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done"
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "incomplete"
        assert card["out_of_scope_segments"] == []
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1
        assert _load_log(app, project["id"])[-1]["scope_verdict"] == "incomplete"


class TestTriggerGates:
    """触发闸（工单 0028 验收 + 0030 修订）：改动代码轮且磁盘确有改动时触发裁判；
    分类判 modify_code 却以 no_change + 磁盘零改动收尾的「可疑零改动轮」同样触发
    （工单 0030：谎称无需改动的唯一逃逸口，裁判带空 diff 裁决）。

    首建轮、咨询轮一律不触发；分类缺席（辅助模型降级）的零改动轮无嫌疑信号，
    不触发——辅助模型注入点零调用即零成本。
    """

    def test_first_build_round_never_triggers_judge(self, app, client, auth_headers):
        """首建轮无旧文件、「越界」无从谈起：分类与裁判都不触发（规格 0021 作用域矩阵）。"""
        utility = use_fake_utility_model(app, [JUDGE_IN_SCOPE_STEP])  # 预排也无从消耗
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {
                    "tool_calls": [
                        ("write_file", {"path": "index.html", "content": "<h1>时钟</h1>"})
                    ]
                },
                _turn_result_step(summary="首建完成。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)

        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        events = confirm_first_build(client, auth_headers, project["id"])

        assert events[-1]["type"] == "done"
        # 首建轮磁盘确有写入，但裁判零调用；卡片无裁决标注（干净收尾）
        assert utility.received_messages == []
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] is None

    def test_consult_round_triggers_zero_judge_calls(self, app, client, auth_headers):
        """咨询轮不进裁判：辅助模型上只有分类一次调用。"""
        utility = use_fake_utility_model(app, [INTENT_CONSULT_STEP, JUDGE_IN_SCOPE_STEP])
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {"text": "标题定义在 index.html 第 1 行。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "标题在哪定义的？")

        assert events[-1]["type"] == "done"
        assert len(utility.received_messages) == 1  # 仅分类，裁判预排步原封未动

    def test_benign_zero_change_round_judged_in_scope(self, app, client, auth_headers):
        """可疑零改动轮（工单 0030）：modify 分类 + no_change 收尾 + 磁盘零改动 → 进裁判。

        良性场景（「确认下标题，不要改。」）由裁判读用户原话判 in_scope，不误伤；
        裁判输入的 diff 为空，占位文案明示无可解码的文本改动。
        """
        utility = use_fake_utility_model(app, [INTENT_MODIFY_STEP, JUDGE_IN_SCOPE_STEP])
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                _turn_result_step(
                    intent="no_change",
                    summary="核实后无需改动。",
                    changed_files=[],
                    no_change_reason="现状已满足诉求。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "确认下标题，不要改。")

        assert events[-1]["type"] == "done"
        assert len(utility.received_messages) == 2  # 分类 1 + 裁判 1
        judge_joined = _joined(utility.received_messages[1])
        assert "确认下标题，不要改。" in judge_joined
        assert "无可解码的文本改动" in judge_joined
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "in_scope"
        assert card["out_of_scope_segments"] == []
        assert events[-1]["scope_verdict"] == "in_scope"
        # 零改动轮不留档快照：硬闸语义不变（不制造假进展版本）
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []


class TestZeroChangeJudgeFallback:
    """可疑零改动轮的裁判兜底（工单 0030）：分类器判 modify_code、模型以 no_change
    收尾且磁盘零改动——谎称「无需改动」的唯一逃逸口，此前拿绿色 CONSISTENT 徽标
    直接过关。裁判带空 diff 照常裁决：诉求要求改动 → incomplete（卡片「未完成」
    徽标）；分类缺席（辅助模型降级）的 no_change 收尾无嫌疑信号，不触发裁判。"""

    def test_false_no_change_on_modify_request_judged_incomplete(
        self, app, client, auth_headers
    ):
        utility = use_fake_utility_model(
            app, [INTENT_MODIFY_STEP, _judge_step(verdict="incomplete")]
        )
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                _turn_result_step(
                    intent="no_change",
                    summary="标题已经改好了。",
                    changed_files=[],
                    no_change_reason="目标状态已存在于磁盘。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改成「原子钟」")

        assert events[-1]["type"] == "done"
        assert len(utility.received_messages) == 2  # 分类 1 + 裁判 1
        card = _last_card(client, auth_headers, project["id"])
        # 逃逸口不再以绿色徽标收官：自洽性仍为 consistent（无申报可比），
        # 但正确性裁决 incomplete——「说改好了但没改」被显式标注
        assert card["consistency"] == "consistent"
        assert card["scope_verdict"] == "incomplete"
        assert card["out_of_scope_segments"] == []
        assert events[-1]["scope_verdict"] == "incomplete"
        # 零改动轮不留档快照（硬闸不变）；裁决写入迭代日志
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []
        assert _load_log(app, project["id"])[-1]["scope_verdict"] == "incomplete"

    def test_prose_fallback_zero_change_also_judged(self, app, client, auth_headers):
        """散文兜底收尾的零改动轮（intent 由系统按磁盘事实合成 no_change）同样进裁判。"""
        utility = use_fake_utility_model(
            app, [INTENT_MODIFY_STEP, _judge_step(verdict="incomplete")]
        )
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {"text": "标题已经改好了。"},
                {"text": "真的改好了。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改成「原子钟」")

        assert events[-1]["type"] == "done"
        assert len(utility.received_messages) == 2
        card = _last_card(client, auth_headers, project["id"])
        assert card["consistency"] == "fallback"
        assert card["scope_verdict"] == "incomplete"

    def test_classifier_absent_zero_change_skips_judge(self, app, client, auth_headers):
        """分类失败静默降级（房规）：无嫌疑信号，零改动轮不进裁判，scope_verdict 为 null。"""
        utility = use_fake_utility_model(app, [RuntimeError("辅助模型不可用")])
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                _turn_result_step(
                    intent="no_change",
                    summary="核实后无需改动。",
                    changed_files=[],
                    no_change_reason="现状已满足诉求。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改成「原子钟」")

        assert events[-1]["type"] == "done"
        # 分类异常快速失败（classify_intent 不重试）：仅 1 次调用，裁判零调用
        assert len(utility.received_messages) == 1
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] is None
        assert "scope_verdict" not in events[-1]


class TestJudgeInputContract:
    """裁判输入契约（工单 0028 验收）：用户原话 + 最近若干轮上下文 + 代码算好的 diff；
    输入禁区：任何智能体自述。"""

    def test_judge_sees_user_words_context_and_code_computed_diff(
        self, app, client, auth_headers
    ):
        utility = use_fake_utility_model(
            app,
            [
                INTENT_MODIFY_STEP,
                JUDGE_IN_SCOPE_STEP,
                INTENT_MODIFY_STEP,
                JUDGE_IN_SCOPE_STEP,
            ],
        )
        use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(summary="第一轮完成。", changed_files=["index.html"]),
                *EDIT_STEPS_V2_V3,
                _turn_result_step(summary="第二轮完成。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        _stream_messages(client, auth_headers, project["id"], "把标题改一下")
        _stream_messages(client, auth_headers, project["id"], "把版本号再改一下")

        # 调用序：分类①、裁判①、分类②、裁判②——裁判②的输入是断言对象
        assert len(utility.received_messages) == 4
        judge_joined = _joined(utility.received_messages[3])
        # 用户原话（本轮）与最近若干轮上下文（历轮用户侧消息）
        assert "把版本号再改一下" in judge_joined
        assert "把标题改一下" in judge_joined
        # diff 由代码算好：文件路径、hunk 头行号定位、前后内容都在
        assert "index.html" in judge_joined
        assert "@@ -1 +1 @@" in judge_joined
        assert "-v2" in judge_joined and "+v3" in judge_joined

    def test_judge_never_sees_agent_self_reports(self, app, client, auth_headers):
        """性质测试：裁判输入不含任何智能体自述（工单 0028 验收）。

        在工程师第一轮 summary 与分类器蒸馏的 user_goal 里植入唯一标记串：
        它们经产物卡片、AI 消息回放与迭代日志扩散到第二轮的各注入面，但
        第二轮裁判调用的消息里必须一个都不出现；且裁判消息里没有 AI 消息
        （历史里的轮次产物结论回放在装配时就地滤除）。
        """
        utility = use_fake_utility_model(
            app,
            [
                _intent_step(user_goal="MARKER-GOAL-7c2 把标题改一下。"),
                JUDGE_IN_SCOPE_STEP,
                INTENT_MODIFY_STEP,
                JUDGE_IN_SCOPE_STEP,
            ],
        )
        use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(
                    summary="MARKER-SUMMARY-9f3 第一轮完成。", changed_files=["index.html"]
                ),
                *EDIT_STEPS_V2_V3,
                _turn_result_step(summary="第二轮完成。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        _stream_messages(client, auth_headers, project["id"], "把标题改一下")
        _stream_messages(client, auth_headers, project["id"], "把版本号再改一下")

        judge_msgs = utility.received_messages[3]
        judge_joined = _joined(judge_msgs)
        # 唯一标记串：工程师 summary 与分类器 user_goal 都不得进入裁判输入
        assert "MARKER-SUMMARY-9f3" not in judge_joined
        assert "MARKER-GOAL-7c2" not in judge_joined
        # 自报意图与申报改动文件（submit_turn_result payload 字样）同样不在
        assert "submit_turn_result" not in judge_joined
        assert "changed_files" not in judge_joined
        # 裁判消息只有系统与用户侧消息：AI 消息（产物结论回放）就地滤除
        assert all(m.type in ("system", "human") for m in judge_msgs)


class TestJudgeFailure:
    """裁判失败语义（工单 0028 验收）：网络/超时/非法输出 → 复用既有重试次数与
    退避语义重试；仍失败 → 标注「核验未完成」且快照照建——安全网故障不得扣住
    用户的劳动成果，且必须显式化而非静默吞掉。"""

    def test_network_failure_retries_then_unverified_with_snapshot(
        self, app, client, auth_headers, monkeypatch
    ):
        monkeypatch.setattr("app.agent.judge.RETRY_BACKOFF_SECONDS", 0)
        utility = use_fake_utility_model(
            app,
            [
                INTENT_MODIFY_STEP,
                RuntimeError("网络抖动"),
                RuntimeError("超时"),
                RuntimeError("仍然失败"),
            ],
        )
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done"
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "unverified"
        assert card["out_of_scope_segments"] == []
        assert events[-1]["scope_verdict"] == "unverified"
        # 快照照建：安全网故障不得扣住用户的劳动成果
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1 and card["snapshot_rev"] == snaps[0]["rev"]
        assert _load_log(app, project["id"])[-1]["scope_verdict"] == "unverified"
        # 分类 1 次 + 裁判 3 次尝试（首次 + 重试 2 次，复用 agent_max_retries=2 语义）
        assert len(utility.received_messages) == 4

    def test_network_failure_then_retry_succeeds(self, app, client, auth_headers, monkeypatch):
        """重试语义是「重试」不是「快速失败」：抖动一次后裁判成功，结论照常呈现。"""
        monkeypatch.setattr("app.agent.judge.RETRY_BACKOFF_SECONDS", 0)
        utility = use_fake_utility_model(
            app, [INTENT_MODIFY_STEP, RuntimeError("网络抖动"), JUDGE_OUT_OF_SCOPE_STEP]
        )
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done"
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "out_of_scope"
        assert len(utility.received_messages) == 3  # 分类 1 + 裁判失败 1 + 重试成功 1

    def test_invalid_output_corrected_once_then_unverified(self, app, client, auth_headers):
        """非法输出回喂错误文案修正一次（房规同分类器）；仍非法 → 「核验未完成」+ 快照照建。"""
        utility = use_fake_utility_model(
            app,
            [
                INTENT_MODIFY_STEP,
                {"text": "我认为这次改动完全在范围内。"},
                {"text": "还是不肯输出 JSON。"},
            ],
        )
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done"
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "unverified"
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1
        # 第二次裁判调用收到了回喂的修正文案（含首次非法输出本身）
        assert len(utility.received_messages) == 3
        refeed_joined = _joined(utility.received_messages[2])
        assert "裁决输出不合法" in refeed_joined
        assert "我认为这次改动完全在范围内。" in refeed_joined

    def test_out_of_scope_without_segments_is_corrected(self, app, client, auth_headers):
        """自相矛盾输出（判越界却给不出段落）按非法处理：回喂修正，修正后的结论作数。"""
        utility = use_fake_utility_model(
            app, [INTENT_MODIFY_STEP, _judge_step(verdict="out_of_scope"), JUDGE_IN_SCOPE_STEP]
        )
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done"
        card = _last_card(client, auth_headers, project["id"])
        assert card["scope_verdict"] == "in_scope"
        assert len(utility.received_messages) == 3


class TestLogRendering:
    """裁决入账后的模型侧渲染（工单 0026 渲染契约延伸到 0028 的新词表值）。"""

    def test_scope_verdict_rendered_into_next_round_prompt(self, app, client, auth_headers):
        """越界结论随迭代日志注入后续轮系统提示，用共享词表的中文文案渲染。"""
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(summary="第一轮完成。", changed_files=["index.html"]),
                _turn_result_step(
                    intent="no_change", summary="无需改动。", no_change_reason="目标状态已存在。"
                ),
            ],
        )
        use_fake_utility_model(
            app,
            [
                INTENT_MODIFY_STEP,
                JUDGE_OUT_OF_SCOPE_STEP,
                INTENT_MODIFY_STEP,
                JUDGE_IN_SCOPE_STEP,  # 第 2 轮是可疑零改动轮：照常进裁判（工单 0030）
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        _stream_messages(client, auth_headers, project["id"], "把标题改一下")
        _stream_messages(client, auth_headers, project["id"], "确认下现状，不要改。")

        # 第二轮系统提示的日志段：第 1 次改动条目带正确性裁决分句
        prompt = model.received_messages[-1][0].content
        line = next(l for l in prompt.splitlines() if "第1次改动" in l)
        assert "核验结论：自洽" in line
        assert "正确性裁决：越界" in line


class TestQuota:
    """成本边界（工单 0028 验收）：裁判不消耗生成名额。"""

    def test_judge_does_not_consume_generation_quota(self, app, client, auth_headers):
        """含裁判的改动轮只扣一个名额：名额在 send_message 接受时按「一轮一次」扣定。"""
        clock = FakeClock()
        app.state.rate_limiter.clock = clock
        app.state.rate_limiter.per_user_hourly = 1
        utility = use_fake_utility_model(app, [INTENT_MODIFY_STEP, JUDGE_IN_SCOPE_STEP])
        use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        assert events[-1]["type"] == "done"
        assert len(utility.received_messages) == 2  # 分类 + 裁判都真实发生了
        # 名额只扣了一次：第 2 条消息才被拒（若裁判也计名额，第 1 轮就会被拒）
        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "再来一次"},
            headers=auth_headers,
        )
        assert resp.status_code == 429
