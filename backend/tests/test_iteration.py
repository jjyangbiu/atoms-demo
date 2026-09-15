"""对话迭代修改端到端测试（工单 0004）。

验收要点：
- 迭代仅修改受影响文件，未涉及文件内容不变（伪模型断言）
- 迭代上下文 = 系统提示（含文件清单）+ 最近 N 条对话 + 当前指令
- 对话历史完整持久化，窗口截断只影响喂给模型的部分
（原「口头完成」守卫的措辞检测与升级链已于工单 0025 整体退役，由自洽性核验
取代——见 tests/test_self_consistency.py。）
任何测试不得调用真实 MiniMax API。
"""

from pathlib import Path

from conftest import (
    FIRST_BUILD_CLARIFY_STEP,
    _turn_result_step,
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
                _turn_result_step(
                    summary="第一版完成。", changed_files=["index.html", "styles.css"]
                ),
                # 迭代轮：先读后改（read-before-write 强制）
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                _turn_result_step(summary="已更新。", changed_files=["index.html"]),
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
        model = use_fake_model(
            app,
            [
                _turn_result_step(
                    intent="no_change", summary=f"ok{i}", no_change_reason="仅确认，无需改动。"
                )
                for i in range(3)
            ],
        )
        project = _create_project(client, auth_headers)
        # 预置文件：三轮都是纯迭代，窗口语义不被首建澄清分流干扰（工单 0015）
        seed_project_files(app, project["id"])
        _stream_messages(client, auth_headers, project["id"], "第一条指令")
        _stream_messages(client, auth_headers, project["id"], "第二条指令")
        _stream_messages(client, auth_headers, project["id"], "第三条指令")

        last_call = model.received_messages[-1]
        contents = [getattr(m, "content", "") for m in last_call]
        assert "第三条指令" in contents  # 当前指令
        # 最近一轮问答在窗口内（工程师回复以轮次产物结论摘要的形式入上下文）
        assert "第二条指令" in contents and any("ok1" in c for c in contents)
        # 更早的被截掉（系统提示里的迭代日志含历史指令子串，用户消息须按精确成员匹配；
        # 轮次产物摘要只注入最近一轮，更早轮次不得出现）
        assert "第一条指令" not in contents
        assert not any("ok0" in c for c in contents)

    def test_full_history_persists_despite_context_window(self, app, settings, client, auth_headers):
        settings.agent_history_window = 1
        use_fake_model(
            app,
            [
                _turn_result_step(
                    intent="no_change", summary=f"ok{i}", no_change_reason="仅确认，无需改动。"
                )
                for i in range(3)
            ],
        )
        project = _create_project(client, auth_headers)
        # 同上：预置文件走纯迭代链路（工单 0015）
        seed_project_files(app, project["id"])
        _stream_messages(client, auth_headers, project["id"], "第一条指令")
        _stream_messages(client, auth_headers, project["id"], "第二条指令")
        _stream_messages(client, auth_headers, project["id"], "第三条指令")

        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        # 工程师回复以轮次产物卡片（JSON，含 summary）持久化，用户消息仍是纯文本
        texts = [
            m["content"] for m in resp.json() if m["kind"] in ("text", "turn_result")
        ]
        for expected in ["第一条指令", "第二条指令", "第三条指令", "ok0", "ok1", "ok2"]:
            assert any(expected in t for t in texts), "窗口截断不得影响持久化完整性"
