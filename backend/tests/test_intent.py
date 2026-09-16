"""意图识别与只读工具集测试（工单 0027 / ADR 0005「第 7 层」）。

接缝沿用规格 0021 既定：
- 主接缝 HTTP API 层：分类分流、咨询轮物理零改动、降级、名额，全部经真实端点断言；
- 辅助接缝「辅助模型注入点」（工单 0027 新增，全仓唯一）：分类器经
  use_fake_utility_model 注入可编程伪模型，received_messages 使
  「分类器输入不含文件内容」「分类调用与工程师主模型隔离」可断言；
- 沙箱单元接缝：只读工具集变体的能力边界直接驱动沙箱验证（先例：编辑/写入工具测试）。

意图只有二值（改动代码 / 咨询）；分类故障静默降级为改动代码，绝不阻断用户轮次
（假阳性有轮内自愈机制——终结出口可自报无需改动，假阴性没有）。
任何测试不得调用真实 MiniMax API。
"""

from pathlib import Path

from conftest import (
    EDIT_STEPS,
    FIRST_BUILD_CLARIFY_STEP,
    INTENT_CONSULT_STEP,
    INTENT_MODIFY_STEP,
    JUDGE_IN_SCOPE_STEP,
    _intent_step,
    _turn_result_step,
    confirm_first_build,
    seed_project_files,
    use_fake_model,
    use_fake_utility_model,
)
from test_generation import _stream_messages
from test_iteration_log import _load_log, _seed_log
from test_projects import _create_project
from test_team_exec import _confirm_tickets
from test_team_tickets import (
    SPEC_TEXT,
    TICKETS_PAYLOAD,
    FakeClock,
    _confirm_consensus,
    _confirm_spec,
)
from test_world import _generate_and_publish, _register_and_login

from app.agent.tools import FileSandbox, build_readonly_tools


class _StubKnowledgeStore:
    """模板知识库桩：只满足 search 约定（同 build_tools 的消费面）。"""

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        return [{"title": "落地页模板", "text": "<h1>落地页骨架</h1>"}]


class TestReadOnlyToolset:
    """只读工具集（沙箱单元接缝）：物理上不含任何写能力。"""

    def test_readonly_toolset_lacks_every_write_capability(self, tmp_path):
        sandbox = FileSandbox(tmp_path)
        (tmp_path / "index.html").write_text("v1", encoding="utf-8")

        names = {t.name for t in build_readonly_tools(sandbox)}

        assert "read_file" in names
        # 写能力与终结出口都不存在于能力清单——不靠劝说，靠能力缺失
        assert not names & {"write_file", "edit_file", "submit_turn_result"}

    def test_readonly_read_file_returns_real_content(self, tmp_path):
        """读取能力真实可用：回答能引用文件内容，不是只给路径+行数+哈希。"""
        sandbox = FileSandbox(tmp_path)
        (tmp_path / "index.html").write_text("<h1>番茄钟</h1>", encoding="utf-8")

        read = next(t for t in build_readonly_tools(sandbox) if t.name == "read_file")

        assert read.func(path="index.html") == "<h1>番茄钟</h1>"

    def test_readonly_toolset_includes_template_search_when_store_available(self, tmp_path):
        sandbox = FileSandbox(tmp_path)

        names = {t.name for t in build_readonly_tools(sandbox, _StubKnowledgeStore())}

        assert names == {"read_file", "search_templates"}


def _project_dir(settings, project_id) -> Path:
    return Path(settings.storage_root) / "projects" / str(project_id)


def _joined(messages: list) -> str:
    """把一次模型调用收到的消息列表拍平成文本（输入契约断言用）。"""
    return "\n".join(str(m.content) for m in messages)


class TestConsultRound:
    """咨询轮（HTTP 主接缝）：只绑只读工具集、流式纯文本收尾、零磁盘改动。"""

    def test_consult_round_streams_answer_with_zero_artifacts(
        self, app, settings, client, auth_headers
    ):
        utility = use_fake_utility_model(app, [INTENT_CONSULT_STEP])
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "styles.css"})]},
                {"text": "标题颜色定义在 styles.css 的 h1 规则里：color: red。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(
            app,
            project["id"],
            {"index.html": "<h1>番茄钟</h1>", "styles.css": "h1{color:red}"},
        )

        events = _stream_messages(
            client, auth_headers, project["id"], "标题颜色是在哪里定义的？"
        )

        # 回答流式原样直达：text 事件在 done 之前出现；done 携完整文本，
        # 且没有产物卡片事件、没有核验结论字段（能力边界决定核验义务）
        types = [e["type"] for e in events]
        assert types[-1] == "done" and "text" in types and "turn_result" not in types
        answer = "标题颜色定义在 styles.css 的 h1 规则里：color: red。"
        assert events[-1]["text"] == answer
        assert "verdict" not in events[-1] and "artifact" not in events[-1]
        # 回答确实读了文件：read_file 工具事件可见（可引用真实文件内容的证据链）
        assert any(e["type"] == "tool" and e["name"] == "read_file" for e in events)

        # 物理零改动：磁盘原样、不留快照、不入迭代日志
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "<h1>番茄钟</h1>"
        assert (pdir / "styles.css").read_text(encoding="utf-8") == "h1{color:red}"
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []
        assert _load_log(app, project["id"]) == []

        # 持久化：纯文本回答消息（刷新可回看），无 turn_result 卡片消息
        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        assert not any(m.get("kind") == "turn_result" for m in messages)
        answers = [m for m in messages if m["role"] == "engineer" and m.get("kind") == "text"]
        assert answers and answers[-1]["content"] == answer

        # 注入点隔离：分类走辅助模型且恰一次；主模型脚本未被分类调用消耗
        assert len(utility.received_messages) == 1
        assert len(model.received_messages) == 2

    def test_consult_round_write_attempt_physically_rejected(
        self, app, settings, client, auth_headers
    ):
        """咨询轮里模型试图写文件：工具不在能力清单，执行报错、磁盘零改动。

        ADR 0005 场景「用户说改回上一版被判咨询」的失败模式对策：不靠劝说，
        靠能力缺失——写调用物理失败，模型只能解释并指向版本历史回滚入口。
        """
        use_fake_utility_model(app, [INTENT_CONSULT_STEP])
        use_fake_model(
            app,
            [
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v2", "new_text": "v1"})
                    ]
                },
                {"text": "咨询轮无法直接改文件，请在版本历史里回滚到上一版。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v2"})

        events = _stream_messages(client, auth_headers, project["id"], "改回上一版")

        edit_events = [
            e for e in events if e["type"] == "tool" and e["name"] == "edit_file"
        ]
        assert edit_events and all(e["status"] != "done" for e in edit_events)
        assert events[-1]["type"] == "done"
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "v2"
        assert _load_log(app, project["id"]) == []

    def test_classifier_input_contract(self, app, client, auth_headers):
        """分类输入只含：文件清单（路径+行数+哈希）、迭代日志、最近对话、用户当前话；
        不注入文件内容，也看不见工程师智能体的自述（注入点隔离）。"""
        utility = use_fake_utility_model(
            app,
            [
                INTENT_CONSULT_STEP,
                _intent_step(
                    intent="modify_code",
                    user_goal="调大标题字号。",
                    target_files=["styles.css"],
                ),
            ],
        )
        model = use_fake_model(
            app,
            [
                # 第 1 轮（咨询）：纯文本回答
                {"text": "标题样式定义在 styles.css 的 h1 规则里。"},
                # 第 2 轮（改动代码）：以无需改动的终结出口收尾即可
                _turn_result_step(
                    intent="no_change",
                    summary="已确认，标题样式保持现状。",
                    no_change_reason="用户只要求确认现状，无需改动。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(
            app,
            project["id"],
            {"index.html": "<h1>MARKER-UNIQ-7331</h1>", "styles.css": "h1{color:MARKER-COLOR}"},
        )
        _seed_log(
            app,
            project["id"],
            [{"seq": 1, "user_text": "此前调整过配色", "files": ["index.html"]}],
        )

        _stream_messages(client, auth_headers, project["id"], "标题颜色是在哪里定义的？")
        _stream_messages(client, auth_headers, project["id"], "确认下标题样式，不要改。")

        assert len(utility.received_messages) == 2
        second = _joined(utility.received_messages[-1])
        # 文件清单（路径+行数+哈希）与用户当前话
        assert "index.html" in second and "哈希" in second
        assert "确认下标题样式，不要改。" in second
        # 迭代日志与最近对话（第 1 轮的问题与咨询回答）
        assert "此前调整过配色" in second
        assert "标题颜色是在哪里定义的？" in second
        assert "标题样式定义在 styles.css 的 h1 规则里。" in second
        # 文件内容不注入；工程师自述（终结出口指令）不可见
        assert "MARKER-UNIQ-7331" not in second and "MARKER-COLOR" not in second
        assert "submit_turn_result" not in second
        # 分类调用不经过主模型：主模型只收到两轮生成各自的调用
        assert len(model.received_messages) == 2


class TestModifyRoundClassification:
    """改动代码轮：分类成功时蒸馏目标与预期文件作上下文注入，日志记 user_goal。"""

    def test_goal_and_target_files_injected_as_advisory_context(
        self, app, settings, client, auth_headers
    ):
        utility = use_fake_utility_model(
            app,
            [
                _intent_step(
                    intent="modify_code", user_goal="把标题调大。", target_files=["styles.css"]
                ),
                JUDGE_IN_SCOPE_STEP,  # 轮末正确性裁判（工单 0028）：改动轮照常触发
            ],
        )
        model = use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "标题大一点")

        # 改动代码轮照常完整执行：卡片、核验、快照、日志一样不少
        assert events[-1]["type"] == "done" and events[-1]["verdict"] == "consistent"
        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert len(snaps) == 1
        log = _load_log(app, project["id"])
        assert len(log) == 1
        assert log[0]["user_text"] == "标题大一点"
        assert log[0]["user_goal"] == "把标题调大。"

        # 分类的预期触及文件注入本轮系统提示：styles.css 磁盘上不存在，只可能来自预判段
        prompt = model.received_messages[0][0].content
        assert "styles.css" in prompt
        # 预期文件只是参考、绝不当沙箱硬闸：申报 styles.css 而实际改 index.html 照常完成
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "v2"
        # 分类 1 次 + 轮末裁判 1 次（工单 0028：改动轮磁盘有变即触发裁判）
        assert len(utility.received_messages) == 2

    def test_user_goal_replaces_truncated_text_in_log_injection(
        self, app, client, auth_headers
    ):
        """分类蒸馏的 user_goal 接管日志注入，替代截断的原话；原话仍完整落库。"""
        long_text = "把首页标题换成深色主题并保持足够的对比度，" * 8 + "尾巴标记END"
        assert len(long_text) > 100
        use_fake_utility_model(
            app,
            [
                _intent_step(
                    intent="modify_code", user_goal="标题改深色主题。", target_files=["index.html"]
                ),
                JUDGE_IN_SCOPE_STEP,  # 第 1 轮轮末裁判（工单 0028）
                _intent_step(
                    intent="modify_code", user_goal="按钮改圆角。", target_files=["styles.css"]
                ),
            ],
        )
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
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], long_text)
        _stream_messages(client, auth_headers, project["id"], "第二次修改")

        # 落库：原话逐字完整，蒸馏目标并列在案
        log = _load_log(app, project["id"])
        assert log[0]["user_text"] == long_text
        assert log[0]["user_goal"] == "标题改深色主题。"

        # 注入：第 2 轮系统提示的日志段用蒸馏目标，不再出现截断原话的尾巴
        prompt = model.received_messages[-1][0].content
        assert "标题改深色主题。" in prompt
        assert "尾巴标记END" not in prompt


class TestClassifierDegradation:
    """分类故障静默降级为改动代码（工单 0027 / ADR 0005）：绝不阻断用户轮次。"""

    def test_invalid_output_corrected_once_then_degrades(
        self, app, settings, client, auth_headers
    ):
        """非法输出回喂错误文案修正一次；仍非法 → 静默降级，轮次照常完整执行。"""
        utility = use_fake_utility_model(
            app,
            [
                {"text": "我觉得这是咨询。"},
                {"text": "intent: consult"},
                JUDGE_IN_SCOPE_STEP,  # 分类降级后轮末裁判照常触发（工单 0028）
            ],
        )
        model = use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "改一下标题")

        # 静默降级为改动代码：完整流水线照常，全程无 error 事件
        types = [e["type"] for e in events]
        assert "error" not in types
        assert types[-1] == "done" and "turn_result" in types
        assert events[-1]["verdict"] == "consistent"
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "v2"
        assert len(_load_log(app, project["id"])) == 1

        # 恰好一次纠错回喂：分类收到两次调用，第二次带「模型输出 + 错误文案」两条
        # 追加；轮末裁判再调一次（工单 0028），共三次
        assert len(utility.received_messages) == 3
        first, second = utility.received_messages[:2]
        assert len(second) == len(first) + 2
        feedback = second[-1]
        assert type(feedback).__name__ == "HumanMessage"
        assert "JSON" in feedback.content

    def test_enum_violation_corrected_once_then_degrades(self, app, client, auth_headers):
        """意图枚举越界（如 question/chitchat）同样走「回喂修正一次 → 降级」。"""
        utility = use_fake_utility_model(
            app,
            [
                {"text": '{"intent": "question", "user_goal": "问问题", "target_files": []}'},
                {"text": '{"intent": "chitchat", "user_goal": "闲聊", "target_files": []}'},
            ],
        )
        model = use_fake_model(
            app,
            [
                _turn_result_step(
                    intent="no_change", summary="无需改动。", no_change_reason="目标状态已存在。"
                )
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "随便问问")

        assert "error" not in [e["type"] for e in events]
        assert events[-1]["type"] == "done" and events[-1]["verdict"] == "consistent"
        assert len(utility.received_messages) == 2
        # 降级为改动代码轮：主模型绑的是完整工具集，脚本正常消耗
        assert len(model.received_messages) == 1

    def test_classifier_exception_degrades_silently(self, app, client, auth_headers):
        """分类调用直接抛异常（如网络故障）：静默降级，不外发任何 error 事件。"""
        utility = use_fake_utility_model(
            app, [RuntimeError("网络抖动"), JUDGE_IN_SCOPE_STEP]  # 轮末裁判照常（工单 0028）
        )
        model = use_fake_model(
            app,
            [*EDIT_STEPS, _turn_result_step(summary="已完成。", changed_files=["index.html"])],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        events = _stream_messages(client, auth_headers, project["id"], "改一下标题")

        assert "error" not in [e["type"] for e in events]
        assert events[-1]["type"] == "done" and events[-1]["verdict"] == "consistent"
        assert len(utility.received_messages) == 2  # 分类异常 1 + 裁判成功 1


class TestScopeGates:
    """分类范围闸（工单 0027 验收）：首建轮与团队工单执行永不进分类器，
    确定性阶段路由原样保留。"""

    def test_first_build_round_never_enters_classifier(self, app, client, auth_headers):
        utility = use_fake_utility_model(app, [INTENT_CONSULT_STEP])  # 预排也无从消耗
        model = use_fake_model(
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

        # 首条消息走澄清（确定性分流原样）：不进分类器
        events = _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        assert any(e["type"] == "consensus" for e in events)
        assert utility.received_messages == []

        # 共识确认后的生成也是首建轮（轮前无文件）：同样不进分类器
        events = confirm_first_build(client, auth_headers, project["id"])
        assert events[-1]["type"] == "done"
        assert utility.received_messages == []
        assert len(model.received_messages) == 3

        # 首建轮入账的迭代日志没有分类蒸馏结果
        log = _load_log(app, project["id"])
        assert len(log) == 1 and log[0]["user_goal"] == ""

    def test_team_ticket_execution_never_enters_classifier(self, app, client, auth_headers):
        """团队工单执行是内部编排而非用户对话轮：全链路不进分类器。"""
        utility = use_fake_utility_model(app, [INTENT_CONSULT_STEP])
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
                _turn_result_step(summary="骨架页面已完成。", changed_files=["index.html"]),
                {
                    "tool_calls": [
                        ("write_file", {"path": "timer.js", "content": "// 计时核心"})
                    ]
                },
                _turn_result_step(summary="计时核心已完成。", changed_files=["timer.js"]),
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        _confirm_spec(client, auth_headers, project["id"])
        events = _confirm_tickets(client, auth_headers, project["id"])

        assert events[-1]["type"] == "done"
        assert utility.received_messages == []

    def test_confirm_driven_implementation_never_enters_classifier(
        self, app, settings, client, auth_headers
    ):
        """确认驱动的实现轮不进分类器（规格确认后直接实现，如克隆的团队项目）。

        确认文案（「确认规格，开始拆解工单。」）在分类器眼里没有改动诉求，一旦
        判为咨询，确定性实现轮就被污染成只读问答——ADR 对首建轮的理由同样适用：
        判为咨询，应用永远建不出来。确定性阶段分流不用 LLM 分类器替换。
        """
        utility = use_fake_utility_model(app, [INTENT_CONSULT_STEP])  # 若进分类器即被污染
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"text": SPEC_TEXT},
                # 规格确认后的直接实现轮：照常写文件收尾
                {"tool_calls": [("write_file", {"path": "app.js", "content": "// 实现"})]},
                _turn_result_step(summary="实现完成。", changed_files=["app.js"]),
            ],
        )
        project = _create_project(client, auth_headers, mode="team")
        _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        _confirm_consensus(client, auth_headers, project["id"])
        # 规格待确认时项目已有文件（克隆等场景）：确认后跳过拆单直接实现
        seed_project_files(app, project["id"], {"index.html": "v1"})
        events = _confirm_spec(client, auth_headers, project["id"])

        assert utility.received_messages == []
        assert events[-1]["type"] == "done"
        assert "turn_result" in [e["type"] for e in events]
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "app.js").read_text(encoding="utf-8") == "// 实现"


class TestQuotaAndClone:
    """成本边界与克隆场景（工单 0027 / ADR 0005）。"""

    def test_one_round_consumes_exactly_one_quota(self, app, client, auth_headers):
        """一个迭代轮无论内部发起几次模型调用，只扣一个名额（分类与裁判都不消耗名额）。"""
        clock = FakeClock()
        app.state.rate_limiter.clock = clock
        app.state.rate_limiter.per_user_hourly = 2
        utility = use_fake_utility_model(
            app, [INTENT_CONSULT_STEP, INTENT_MODIFY_STEP, JUDGE_IN_SCOPE_STEP]
        )
        model = use_fake_model(
            app,
            [
                # 第 1 轮（咨询）：分类 1 次 + 回答 1 次
                {"text": "标题定义在 index.html 的 h1 里。"},
                # 第 2 轮（改动代码）：分类 1 次 + 读 + 改 + 出口共 3 次主模型调用
                *EDIT_STEPS,
                _turn_result_step(summary="已完成。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})

        first = _stream_messages(client, auth_headers, project["id"], "标题在哪定义的？")
        second = _stream_messages(client, auth_headers, project["id"], "把标题改一下")
        assert first[-1]["type"] == "done" and second[-1]["type"] == "done"
        # 两轮共 2 次分类 + 1 次裁判（工单 0028：第 2 轮磁盘有变）+ 4 次主模型调用；
        # 若分类/裁判也计名额，第 2 轮就该 429 了
        assert len(utility.received_messages) == 3
        assert len(model.received_messages) == 4

        # 名额恰好用尽：第 3 条消息被拒
        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "再来一次"},
            headers=auth_headers,
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["reason"] == "user_hourly"

    def test_clone_first_message_enters_classifier(self, app, settings, client, auth_headers):
        """克隆项目的首条消息就是普通迭代轮：照常进分类器。

        ADR 0005 场景「改回上一版」：判咨询 → 只读轮解释并指向回滚入口，磁盘零改动。
        """
        project, pub = _generate_and_publish(
            app, client, auth_headers, content="<h1>v1</h1>"
        )
        eve_headers = _register_and_login(client, "eve")
        cloned = client.post(f"/api/world/{pub['slug']}/clone", headers=eve_headers).json()

        utility = use_fake_utility_model(app, [INTENT_CONSULT_STEP])
        use_fake_model(
            app,
            [{"text": "克隆副本暂无可回退的历史版本，源项目的版本不随克隆携带。"}],
        )

        events = _stream_messages(client, eve_headers, cloned["id"], "改回上一版")

        # 照常进分类器，且判咨询后走只读轮：无卡片、磁盘零改动、不入日志
        assert len(utility.received_messages) == 1
        types = [e["type"] for e in events]
        assert types[-1] == "done" and "turn_result" not in types
        pdir = _project_dir(settings, cloned["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "<h1>v1</h1>"
        assert _load_log(app, cloned["id"]) == []
