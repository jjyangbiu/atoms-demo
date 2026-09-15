"""多轮对话“口头完成”治理的端到端回归测试（诊断修复第二轮：硬闸设计）。

用户报告的症状：经过多轮对话后，经常性出现 LLM 回复“已修改/已完成”，
但文件实际没有任何改动。第一轮修复（反思回喂、断言词表、空操作拒绝）被指出
“没治本，完全依赖提示词对大模型的软约束”——本轮把保证从模型行为移到系统不变量
（ADR 0004：提示词是建议性的，物理强制不依赖模型自觉）：

- 硬输出闸：零改动轮的持久化文本由系统无条件追加核验标注（断言检测只决定
  措辞强度，检测漏了“无改动”的事实照样落库）——裸声称无法单独存活于历史。
- 快照硬闸：零改动轮不建快照，轮次结局以磁盘事实为准，不制造假进展。
- 事实注入升级链（loop.run_generation）：第 1 次口头完成回喂反思；第 2 次注入
  系统直读磁盘的权威事实（文件当前内容+历轮实际改动记录）；第 3 次放行收尾，
  由硬输出闸兜底真实性。
- 结构化结局：done 事件附 no_change 标志，前端可持久化渲染（弹窗是一过性的）。
- 空操作拒绝（tools.edit_file）：old_text == new_text 的零 diff 不计为成功修改。

一个口头完成到底的轮次消耗 3 次模型调用：声称→反思→声称→事实注入→声称→done。
任何测试不得调用真实 MiniMax API（规格 0001 Testing Decisions）。
"""

from pathlib import Path

from conftest import seed_project_files, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project

from app.agent.loop import claims_completion


def _project_dir(settings, project_id) -> Path:
    return Path(settings.storage_root) / "projects" / str(project_id)


def _engineer_texts(client, auth_headers, project_id) -> list[str]:
    resp = client.get(f"/api/projects/{project_id}/messages", headers=auth_headers)
    return [
        m["content"] for m in resp.json() if m["role"] == "engineer" and m["kind"] == "text"
    ]


class TestMultiTurnVerbalCompletion:
    def test_disputed_claim_gets_persisted_correction(
        self, app, settings, client, auth_headers
    ):
        """H1 主场景：口头完成 → 用户质疑 → 模型升级链走完仍声称“已生效”。

        不变量（用户视角）：对话结束时，要么文件真的改成了，要么持久化历史里
        存在与声称成对出现的系统核验标注——回看与后续轮上下文都不再只有谎言。
        标注由系统代码无条件追加，与模型是否配合无关。
        """
        use_fake_model(
            app,
            [
                # --- 轮 1：口头完成，反思、事实注入后仍不改（升级链 3 步） ---
                {"text": "已修改完成，标题已更新为『工作日历』。"},
                {"text": "经核对，改动已在上一轮生效，无需重复修改。"},
                {"text": "再次确认：改动已在上一轮生效。"},
                # --- 轮 2：用户质疑，模型被轮1历史污染，继续声称到底 ---
                {"text": "上一轮已经完成修改，标题现在是『工作日历』，请刷新页面查看。"},
                {"text": "已确认改动生效，若仍显示旧标题请强制刷新浏览器缓存。"},
                {"text": "改动确实已生效，无需再改。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历-Test</h1>"})

        _stream_messages(client, auth_headers, project["id"], "把标题改成工作日历")
        _stream_messages(
            client, auth_headers, project["id"], "你根本没改，标题还是工作日历-Test，请实际修改"
        )

        content = (_project_dir(settings, project["id"]) / "index.html").read_text(
            encoding="utf-8"
        )
        file_actually_changed = "工作日历-Test" not in content

        engineer_texts = _engineer_texts(client, auth_headers, project["id"])
        persisted_correction = any("系统核验" in t for t in engineer_texts)

        assert file_actually_changed or persisted_correction, (
            f"两轮对话后文件未改，且持久化历史里只有裸声称 {engineer_texts!r}，"
            "无系统核验标注；这些声称将作为 AIMessage 回灌后续轮次上下文"
        )
        # 声称轮都带核验标注：声称与纠正必须成对持久化
        claim_texts = [t for t in engineer_texts if "生效" in t or "完成修改" in t]
        assert claim_texts and all("系统核验" in t for t in claim_texts)

    def test_correction_marker_travels_into_next_turn_context(
        self, app, settings, client, auth_headers
    ):
        """H1 上下文回灌腿：核验标注必须随声称一起进入后续轮的模型上下文。"""
        model = use_fake_model(
            app,
            [
                # 轮 1：升级链 3 步走完，最后一条声称+核验标注成对持久化
                {"text": "已修改完成，标题已更新为『工作日历』。"},
                {"text": "经核对，改动已在上一轮生效，无需重复修改。"},
                {"text": "再次确认：改动已在上一轮生效。"},
                # 轮 2
                {"text": "好的，我先读取文件确认现状。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历-Test</h1>"})
        _stream_messages(client, auth_headers, project["id"], "把标题改成工作日历")
        _stream_messages(client, auth_headers, project["id"], "确认一下标题现在是什么")

        turn2_messages = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in turn2_messages]
        claim_in_context = any("改动已在上一轮生效" in c for c in contents)
        correction_in_context = any("系统核验" in c for c in contents)
        assert claim_in_context, "声称文本应进入下一轮上下文（回灌路径存在）"
        assert correction_in_context, "声称进入上下文时必须伴随系统核验标注"

    def test_claim_wording_outside_literal_list_triggers_reflection(
        self, app, settings, client, auth_headers
    ):
        """H2：“已将标题替换为…”不在固定词表内，泛化句式族仍须触发反思守卫。"""
        model = use_fake_model(
            app,
            [{"text": "好的，已将标题替换为『工作日历』，刷新页面即可看到效果。"}],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历-Test</h1>"})
        _stream_messages(client, auth_headers, project["id"], "把标题改成工作日历")

        reflection_fed = len(model.received_messages) >= 2 and any(
            "系统检查" in getattr(m, "content", "") for m in model.received_messages[1]
        )
        assert reflection_fed, "『已将…替换为…』措辞绕过了断言检测，守卫未触发反思"

    def test_noop_edit_round_still_warns(self, app, settings, client, auth_headers):
        """H3：old_text == new_text 的空操作 edit_file 不得计为成功修改。

        零 diff 轮次必须与“未调工具”同等对待：done 附 warning + no_change、文件零变化。
        """
        use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        (
                            "edit_file",
                            {
                                "path": "index.html",
                                "old_text": "<h1>工作日历</h1>",
                                "new_text": "<h1>工作日历</h1>",
                            },
                        )
                    ]
                },
                {"text": "已修改完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历</h1>"})
        events = _stream_messages(client, auth_headers, project["id"], "把标题改成工作日历")

        # 空操作 edit_file 以 error 收尾（前端显示 ✗ 而非 ✓）
        edit_events = [
            e for e in events if e["type"] == "tool" and e["name"] == "edit_file"
        ]
        assert edit_events and edit_events[-1]["status"] == "error"
        assert "空操作" in edit_events[-1].get("result", "")

        done_events = [e for e in events if e["type"] == "done"]
        assert done_events, "应有 done 事件收尾"
        assert done_events[-1].get("warning") == "本轮未产生任何文件改动"
        assert done_events[-1].get("no_change") is True
        # 磁盘零变化
        assert (
            _project_dir(settings, project["id"]) / "index.html"
        ).read_text(encoding="utf-8") == "<h1>工作日历</h1>"


class TestHardOutputGate:
    """硬输出闸：零改动轮的真实性保证在系统代码，不在断言检测器。"""

    def test_undetected_claim_wording_still_gets_factual_annotation(
        self, app, settings, client, auth_headers
    ):
        """断言检测漏报的措辞，零改动事实照样无条件落库（软约束→硬闸的核心差异）。

        “一切都已就绪”不含任何完成断言句式，claims_completion 检不出——
        但系统不依赖检测：只要磁盘零改动，持久化文本必带系统记录标注。
        """
        text = "一切都已就绪，请刷新页面查看。"
        assert not claims_completion(text), "该措辞应检测不出，才能证明硬闸不依赖检测器"
        use_fake_model(app, [{"text": text}])
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历-Test</h1>"})
        events = _stream_messages(client, auth_headers, project["id"], "把标题改成工作日历")

        persisted = _engineer_texts(client, auth_headers, project["id"])[-1]
        assert persisted.startswith(text)
        assert "系统记录：本轮未产生文件改动" in persisted
        done_events = [e for e in events if e["type"] == "done"]
        assert done_events[-1].get("no_change") is True

    def test_zero_change_round_creates_no_snapshot(self, app, settings, client, auth_headers):
        """快照硬闸：零改动轮不建快照，不给用户制造“版本 N+1”的假进展。"""
        use_fake_model(
            app,
            [
                # 轮 1：先读后改，真实改动 → 快照 rev1
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
                    ]
                },
                {"text": "构建完成。"},
                # 轮 2：纯文本零改动 → 不建快照
                {"text": "你好，需要我做什么？"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "建个页面")
        snaps_after_round1 = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        _stream_messages(client, auth_headers, project["id"], "你好")
        snaps_after_round2 = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()

        assert [s["rev"] for s in snaps_after_round1] == [1]
        assert [s["rev"] for s in snaps_after_round2] == [1], "零改动轮不得创建新快照"


class TestFactsEscalation:
    """升级链第二级：系统直读磁盘的硬事实注入（替代纯提示词劝说）。"""

    def test_second_claim_injects_disk_facts(self, app, settings, client, auth_headers):
        """第 2 次口头完成时，注入内容必须是系统刚从磁盘读取的实际状态。"""
        model = use_fake_model(
            app,
            [
                {"text": "已修改完成，标题已更新为『工作日历』。"},
                {"text": "改动已生效，请刷新查看。"},
                {"text": "我坚持：改动已生效。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历-Test</h1>"})
        _stream_messages(client, auth_headers, project["id"], "把标题改成工作日历")

        assert len(model.received_messages) >= 3, "升级链应走到第 3 次模型调用"
        facts_call = model.received_messages[2]
        contents = [getattr(m, "content", "") for m in facts_call]
        assert any("系统核查" in c for c in contents), "第 2 次声称应触发事实注入"
        # 注入的是磁盘真实内容（用户视角的谎言克星：文件里仍是 -Test 后缀）
        assert any("工作日历-Test" in c for c in contents), "事实注入必须含文件当前磁盘内容"
        assert any("index.html 当前磁盘内容" in c for c in contents)

    def test_model_recovers_after_facts_injection(self, app, settings, client, auth_headers):
        """面对磁盘事实后模型据实修改 → 文件真改、无标注、无 warning（恢复路径）。"""
        use_fake_model(
            app,
            [
                {"text": "已修改完成，标题已更新为『工作日历』。"},
                {"text": "改动已生效。"},
                # 事实注入后：模型看到磁盘上还是 -Test，据实修改
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        (
                            "edit_file",
                            {
                                "path": "index.html",
                                "old_text": "工作日历-Test",
                                "new_text": "工作日历",
                            },
                        )
                    ]
                },
                {"text": "已将标题改为工作日历。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历-Test</h1>"})
        events = _stream_messages(client, auth_headers, project["id"], "把标题改成工作日历")

        assert (
            _project_dir(settings, project["id"]) / "index.html"
        ).read_text(encoding="utf-8") == "<h1>工作日历</h1>"
        done_events = [e for e in events if e["type"] == "done"]
        assert done_events and "warning" not in done_events[-1]
        assert not done_events[-1].get("no_change")
        persisted = _engineer_texts(client, auth_headers, project["id"])[-1]
        assert persisted == "已将标题改为工作日历。", "真实改动轮不得被追加核验标注"


class TestClaimsCompletionDetection:
    """claims_completion 的措辞覆盖：宁偶误报（多一次反思），不漏报口头完成。"""

    def test_claim_wordings_detected(self):
        claims = [
            "已修改完成，标题已更新为『工作日历』。",
            "好的，已将标题替换为『工作日历』，刷新页面即可看到效果。",
            "改动已生效，请刷新查看。",
            "已经帮你把标题调整好了。",
            "上一轮已经完成修改。",
            "标题已更新。",
            "修改完毕。",
            "已添加统计区。",
            "搞定了，已将样式优化。",
        ]
        for text in claims:
            assert claims_completion(text), f"漏报口头完成: {text!r}"

    def test_non_claim_wordings_not_detected(self):
        non_claims = [
            "你好，需要我做什么？",
            "如果你已确认，我就开始修改。",
            "我可以帮你修改标题。",
            "本轮未产生任何文件改动。",
            "计划分三步完成。",
            "index.html 已存在，修改请走 edit_file。",
            "请告诉我你想要的样式。",
        ]
        for text in non_claims:
            assert not claims_completion(text), f"误报口头完成: {text!r}"
