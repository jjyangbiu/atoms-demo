"""对话迭代修改端到端测试（工单 0004）。

验收要点：
- 迭代仅修改受影响文件，未涉及文件内容不变（伪模型断言）
- 迭代上下文 = 系统提示（含文件清单）+ 最近 N 条对话 + 当前指令
- 对话历史完整持久化，窗口截断只影响喂给模型的部分
- “口头完成”守卫：模型未调用工具就在文本里声称已修改时，循环反思回喂一次；
  若仍不改，done 事件附 warning 提醒前端（诊断修复）
任何测试不得调用真实 MiniMax API。
"""

from pathlib import Path

from conftest import (
    FIRST_BUILD_CLARIFY_STEP,
    confirm_first_build,
    seed_project_files,
    use_fake_model,
)
from test_generation import _stream_messages
from test_projects import _create_project


def _project_dir(settings, project_id) -> Path:
    return Path(settings.storage_root) / "projects" / str(project_id)


class TestIteration:
    def test_iteration_only_touches_affected_files(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                # 首建：澄清产出共识，确认后生成两个文件（工单 0015）
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"tool_calls": [("write_file", {"path": "styles.css", "content": "body{}"})]},
                {"text": "第一版完成。"},
                # 迭代轮：先读后改（read-before-write 强制）
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "做一个页面")
        confirm_first_build(client, auth_headers, project["id"])
        events = _stream_messages(client, auth_headers, project["id"], "把标题改一下")

        # 迭代轮只触碰受影响文件
        touched = {
            e["args"]["path"]
            for e in events
            if e["type"] == "tool" and e["status"] == "start"
        }
        assert touched == {"index.html"}

        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "v2"
        # 未涉及文件内容保持不变
        assert (pdir / "styles.css").read_text(encoding="utf-8") == "body{}"

    def test_history_window_keeps_recent_messages_only(self, app, settings, client, auth_headers):
        settings.agent_history_window = 1  # 仅保留最近一轮问答
        model = use_fake_model(app, [{"text": f"ok{i}"} for i in range(3)])
        project = _create_project(client, auth_headers)
        # 预置文件：三轮都是纯迭代，窗口语义不被首建澄清分流干扰（工单 0015）
        seed_project_files(app, project["id"])
        _stream_messages(client, auth_headers, project["id"], "第一条指令")
        _stream_messages(client, auth_headers, project["id"], "第二条指令")
        _stream_messages(client, auth_headers, project["id"], "第三条指令")

        last_call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in last_call]
        assert "第三条指令" in contents  # 当前指令
        assert "第二条指令" in contents and "ok1" in contents  # 最近一轮问答在窗口内
        assert "第一条指令" not in contents and "ok0" not in contents  # 更早的被截掉

    def test_full_history_persists_despite_context_window(self, app, settings, client, auth_headers):
        settings.agent_history_window = 1
        use_fake_model(app, [{"text": f"ok{i}"} for i in range(3)])
        project = _create_project(client, auth_headers)
        # 同上：预置文件走纯迭代链路（工单 0015）
        seed_project_files(app, project["id"])
        _stream_messages(client, auth_headers, project["id"], "第一条指令")
        _stream_messages(client, auth_headers, project["id"], "第二条指令")
        _stream_messages(client, auth_headers, project["id"], "第三条指令")

        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        texts = [m["content"] for m in resp.json() if m["kind"] == "text"]
        for expected in ["第一条指令", "第二条指令", "第三条指令", "ok0", "ok1", "ok2"]:
            assert expected in texts, "窗口截断不得影响持久化完整性"


class TestVerbalCompletionGuard:
    """“口头完成”守卫（诊断修复）。

    背景：用户报告“把标题从『工作日历-Test』改成『工作日历』”，模型未调 edit_file 就
    回复“已改好”，旧实现无条件发 done，文件未动、快照照旧创建，用户被误导。

    防御：
    1) loop.run_generation 发现“模型声称完成 + 本轮无任何修改类工具成功”时，回喂一次反思；
    2) 反思后仍不改，done 事件附 warning 字段提醒前端；
    3) 合法闲聊轮（文本不含完成断言词）不触发任何防御。
    """

    def test_reflection_recovers_when_model_complies(self, app, settings, client, auth_headers):
        """模型先“口头完成”，反思后乖乖调工具 → 文件真改了、done 不带 warning。"""
        model = use_fake_model(
            app,
            [
                # 迭代轮第一步：未调工具，直接声称已改好 → 应触发反思
                {"text": "已修改完成。"},
                # 反思后模型乖乖先 read 再 edit
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
        events = _stream_messages(
            client, auth_headers, project["id"], "把标题改成工作日历"
        )

        # 反思消息已回喂给模型（received_messages 第二步首条 HumanMessage 含“系统检查”）
        assert len(model.received_messages) >= 2
        reflection_turn = model.received_messages[1]
        assert any(
            "系统检查" in getattr(m, "content", "") for m in reflection_turn
        ), "未向模型回喂反思消息"

        # 文件真改了
        pdir = _project_dir(settings, project["id"])
        assert (
            pdir / "index.html"
        ).read_text(encoding="utf-8") == "<h1>工作日历</h1>"

        # done 事件存在且不带 warning（touched_files 非空）
        done_events = [e for e in events if e["type"] == "done"]
        assert done_events and "warning" not in done_events[-1]

    def test_persistent_verbal_completion_emits_warning(self, app, settings, client, auth_headers):
        """模型反思后仍不改 → done 事件附 warning，文件保持原样。"""
        use_fake_model(
            app,
            [
                {"text": "已修改完成。"},  # 首次口头完成 → 触发反思
                {"text": "已完成。"},  # 反思后仍口头完成 → 不再回喂（防死循环），done + warning
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>工作日历-Test</h1>"})
        events = _stream_messages(
            client, auth_headers, project["id"], "把标题改成工作日历"
        )

        # 文件未被改动
        pdir = _project_dir(settings, project["id"])
        assert (
            pdir / "index.html"
        ).read_text(encoding="utf-8") == "<h1>工作日历-Test</h1>"

        # done 事件带 warning，前端得以提醒用户
        done_events = [e for e in events if e["type"] == "done"]
        assert done_events, "反思后仍应发 done 事件收尾"
        assert done_events[-1].get("warning") == "本轮未产生任何文件改动"

    def test_chat_only_round_skips_reflection_but_still_warns(self, app, settings, client, auth_headers):
        """合法闲聊轮（文本不含完成断言词）不触发反思回喂；
        但因本轮未改动文件，done 仍附 warning（选项 A 的完整语义：只要未改就提醒）。"""
        model = use_fake_model(app, [{"text": "你好，需要我做什么？"}])
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "<h1>hi</h1>"})
        events = _stream_messages(client, auth_headers, project["id"], "你好")

        # 未回喂反思（只有一次模型调用）
        assert len(model.received_messages) == 1
        # done 事件仍带 warning（touched_files 为空）
        done_events = [e for e in events if e["type"] == "done"]
        assert done_events and done_events[-1].get("warning") == "本轮未产生任何文件改动"
