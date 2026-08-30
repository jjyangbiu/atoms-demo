"""生成主链路端到端测试：伪模型驱动 → SSE 事件 → 多文件落盘 → 持久化。

验收对应工单 0003：任何测试不得调用真实 MiniMax API。
"""

import json
from pathlib import Path

from conftest import use_fake_model
from test_projects import _create_project


def _stream_messages(client, headers, project_id, content):
    """发送消息并解析 SSE 流，返回事件列表。"""
    with client.stream(
        "POST",
        f"/api/projects/{project_id}/messages",
        json={"content": content},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return [
            json.loads(line.removeprefix("data: "))
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]


def _project_dir(settings, project_id) -> Path:
    return Path(settings.storage_root) / "projects" / str(project_id)


class TestGeneration:
    def test_full_generation_writes_files_and_streams_events(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>时钟</h1>"})]},
                {"tool_calls": [("write_file", {"path": "styles.css", "content": "h1{color:red}"})]},
                {"text": "已完成：一个包含入口页与样式的时钟应用。"},
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "做一个时钟应用")

        types = [e["type"] for e in events]
        # 事件序列：工具 start/done 成对 → 最终文本 → done（持久化完成后才发出，且是最后一个事件）
        assert types.count("tool") == 4
        assert "text" in types and types[-1] == "done"
        write_starts = [e for e in events if e["type"] == "tool" and e["status"] == "start"]
        assert [e["args"]["path"] for e in write_starts] == ["index.html", "styles.css"]

        # 文件真实落盘且内容正确
        pdir = _project_dir(settings, project["id"])
        assert (pdir / "index.html").read_text(encoding="utf-8") == "<h1>时钟</h1>"
        assert (pdir / "styles.css").read_text(encoding="utf-8") == "h1{color:red}"

    def test_messages_persisted_after_generation(self, app, client, auth_headers):
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>hi</h1>"})]},
                {"text": "构建完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "随便做个页面")

        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        messages = resp.json()
        assert messages[0]["role"] == "user" and messages[0]["content"] == "随便做个页面"
        kinds = [m["kind"] for m in messages]
        assert "event" in kinds  # 工具事件留痕
        assert messages[-1]["role"] == "engineer" and messages[-1]["content"] == "构建完成。"

    def test_iteration_carries_history_and_file_listing(self, app, client, auth_headers):
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "第一版完成。"},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "第一版")
        _stream_messages(client, auth_headers, project["id"], "改一下")

        # 第二轮的上下文：系统提示含已有文件清单，历史含第一轮问答
        second_call = model.received_messages[-1]
        assert "index.html" in second_call[0].content
        contents = [getattr(m, "content", "") for m in second_call]
        assert "第一版" in contents and "第一版完成。" in contents and "改一下" in contents
        # 增量修改生效
        assert (
            _project_dir(app.state.settings, project["id"]) / "index.html"
        ).read_text(encoding="utf-8") == "v2"

    def test_path_traversal_is_blocked(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "../../evil.html", "content": "bad"})]},
                {"text": "尝试越界。"},
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "越界")

        tool_done = [e for e in events if e["type"] == "tool" and e["status"] == "error"]
        assert tool_done, "越界写入应产生错误状态的工具事件"
        assert not (Path(settings.storage_root).parent / "evil.html").exists()
        assert not (Path(settings.storage_root) / "evil.html").exists()

    def test_extension_whitelist_is_enforced(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                {"tool_calls": [("write_file", {"path": "app.exe", "content": "bad"})]},
                {"text": "尝试写可执行文件。"},
            ],
        )
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "写个exe")
        assert any(e["type"] == "tool" and e["status"] == "error" for e in events)
        assert not (_project_dir(settings, project["id"]) / "app.exe").exists()

    def test_model_failure_exhausts_retries_then_error_event(self, app, settings, client, auth_headers):
        settings.agent_max_retries = 2
        use_fake_model(app, [RuntimeError, RuntimeError, RuntimeError])
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "注定失败")
        assert events[-1]["type"] == "error"
        assert "模型调用失败" in events[-1]["detail"]

    def test_llm_not_configured_surfaces_error_event(self, settings, client, auth_headers):
        # 不注入伪模型、也无 API Key：默认工厂应报错并以 error 事件收尾，而非 500
        settings.llm_api_key = ""
        project = _create_project(client, auth_headers)
        events = _stream_messages(client, auth_headers, project["id"], "你好")
        assert events[-1]["type"] == "error"
        assert "ATOMS_LLM_API_KEY" in events[-1]["detail"]

    def test_send_message_requires_owner(self, app, client, auth_headers):
        use_fake_model(app, [{"text": "ok"}])
        project = _create_project(client, auth_headers)
        client.post("/api/auth/register", json={"username": "eve", "password": "secret123"})
        resp = client.post(
            "/api/auth/login", json={"username": "eve", "password": "secret123"}
        )
        eve_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        assert (
            client.post(
                f"/api/projects/{project['id']}/messages",
                json={"content": "偷生成"},
                headers=eve_headers,
            ).status_code
            == 404
        )
