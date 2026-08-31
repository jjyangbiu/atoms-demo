"""生成主链路端到端测试：伪模型驱动 → SSE 事件 → 多文件落盘 → 持久化。

验收对应工单 0003：任何测试不得调用真实 MiniMax API。
"""

import asyncio
import json
from pathlib import Path

from conftest import (
    FIRST_BUILD_CLARIFY_STEP,
    confirm_first_build,
    seed_project_files,
    use_fake_model,
)
from fake_model import FakeStreamingModel
from test_projects import _create_project

from app.agent.loop import THINK_CLOSE, THINK_OPEN, run_generation


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
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>时钟</h1>"})]},
                {"tool_calls": [("write_file", {"path": "styles.css", "content": "h1{color:red}"})]},
                {"text": "已完成：一个包含入口页与样式的时钟应用。"},
            ],
        )
        project = _create_project(client, auth_headers)
        # 首建流水线（工单 0015）：首条消息先产出需求共识，确认后才开始生成
        clarify_events = _stream_messages(client, auth_headers, project["id"], "做一个时钟应用")
        assert any(e["type"] == "consensus" for e in clarify_events)
        events = confirm_first_build(client, auth_headers, project["id"])

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
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>hi</h1>"})]},
                {"text": "构建完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "随便做个页面")
        confirm_first_build(client, auth_headers, project["id"])

        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        messages = resp.json()
        assert messages[0]["role"] == "user" and messages[0]["content"] == "随便做个页面"
        kinds = [m["kind"] for m in messages]
        assert "consensus" in kinds and "consensus_confirm" in kinds  # 澄清与确认门留痕可回看
        assert "event" in kinds  # 工具事件留痕
        assert messages[-1]["role"] == "engineer" and messages[-1]["content"] == "构建完成。"

    def test_iteration_carries_history_and_file_listing(self, app, client, auth_headers):
        model = use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "index.html", "content": "v1"})]},
                {"text": "第一版完成。"},
                {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})]},
                {"text": "已更新。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "第一版")
        confirm_first_build(client, auth_headers, project["id"])
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
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "../../evil.html", "content": "bad"})]},
                {"text": "尝试越界。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "越界")
        events = confirm_first_build(client, auth_headers, project["id"])

        tool_done = [e for e in events if e["type"] == "tool" and e["status"] == "error"]
        assert tool_done, "越界写入应产生错误状态的工具事件"
        assert not (Path(settings.storage_root).parent / "evil.html").exists()
        assert not (Path(settings.storage_root) / "evil.html").exists()

    def test_extension_whitelist_is_enforced(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                FIRST_BUILD_CLARIFY_STEP,
                {"tool_calls": [("write_file", {"path": "app.exe", "content": "bad"})]},
                {"text": "尝试写可执行文件。"},
            ],
        )
        project = _create_project(client, auth_headers)
        _stream_messages(client, auth_headers, project["id"], "写个exe")
        events = confirm_first_build(client, auth_headers, project["id"])
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


class _GatedStreamModel:
    """前半段流式产出后卡在闸门上，用于断言事件是实时外发而非整段缓冲。"""

    def __init__(self, first_chunks: list[str], rest_chunks: list[str], gate: asyncio.Event):
        self.first_chunks = first_chunks
        self.rest_chunks = rest_chunks
        self.gate = gate

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        for piece in self.first_chunks:
            yield AIMessageChunk(content=piece)
        await self.gate.wait()
        for piece in self.rest_chunks:
            yield AIMessageChunk(content=piece)


class TestThinkingStream:
    """思考过程展示（诊断修复）：MiniMax-M3 把思考以 <think>...</think> 行内输出，
    须拆为独立 thinking 事件（前端才能以小号可折叠样式展示），且事件实时外发（打字机效果前提）。
    """

    def test_think_tags_split_into_thinking_events_across_chunks(self, app, client, auth_headers):
        # 标签故意被切断在 chunk 边界上（开标签剩前半截 + 闭标签剩前半截）
        app.state.model_factory = lambda _s: FakeStreamingModel(
            [[THINK_OPEN[:3], THINK_OPEN[3:] + "先分析需求。" + THINK_CLOSE[:4], THINK_CLOSE[4:] + "构建完成。"]]
        )
        project = _create_project(client, auth_headers)
        # 逐字流式伪模型无法发工具调用：预置文件绕过澄清分流，直测工程师流（工单 0015）
        seed_project_files(app, project["id"])
        events = _stream_messages(client, auth_headers, project["id"], "做一个时钟")

        think = "".join(e["content"] for e in events if e["type"] == "thinking")
        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert think == "先分析需求。"
        assert text == "构建完成。"
        # 结论（done 与落库文本）不残留思考标签与内容
        assert events[-1]["type"] == "done"
        assert events[-1]["text"] == "构建完成。"

    def test_thinking_persisted_and_excluded_from_context(self, app, client, auth_headers):
        model = FakeStreamingModel(
            [
                [THINK_OPEN + "先分析需求。" + THINK_CLOSE + "构建完成。"],
                ["已更新。"],
            ]
        )
        app.state.model_factory = lambda _s: model
        project = _create_project(client, auth_headers)
        # 同上：预置文件绕过澄清分流（逐字流式伪模型无法发工具调用，工单 0015）
        seed_project_files(app, project["id"])
        _stream_messages(client, auth_headers, project["id"], "做一个页面")

        messages = client.get(
            f"/api/projects/{project['id']}/messages", headers=auth_headers
        ).json()
        kinds = [m["kind"] for m in messages]
        assert "thinking" in kinds
        thinking_row = next(m for m in messages if m["kind"] == "thinking")
        assert thinking_row["content"] == "先分析需求。"
        assert messages[-1]["role"] == "engineer" and messages[-1]["content"] == "构建完成。"

        # 迭代一轮：思考行不入模型上下文（与工具事件行同等对待）
        _stream_messages(client, auth_headers, project["id"], "改一下")
        second_call = [getattr(m, "content", "") for m in model.received_messages[-1]]
        assert not any("先分析需求" in c for c in second_call)
        assert "构建完成。" in second_call

    def test_text_events_stream_live_not_buffered(self):
        """打字机效果的前提：模型还在流式产出时，前面的增量就已外发（而非整段缓冲后一次性 flush）。"""

        async def scenario():
            gate = asyncio.Event()
            model = _GatedStreamModel(["你好"], ["，世界。"], gate)
            gen = run_generation(model, [], None, "系统提示", [], "打个招呼")
            # 闸门未开（模型后半段还没产出），第一个事件必须已可拿到；超时即说明仍是整段缓冲
            first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
            assert first.type == "text" and first.data["content"] == "你好"
            gate.set()
            rest = [e async for e in gen]
            assert any(e.type == "text" and e.data["content"] == "，世界。" for e in rest)
            assert rest[-1].type == "done" and rest[-1].data["text"] == "你好，世界。"

        asyncio.run(scenario())

    def test_streaming_failure_after_emission_is_not_retried(self):
        """已外发内容后中途失败：不得重试（避免半截流重复），以 error 收尾。"""

        class _MidFailModel:
            def bind_tools(self, tools):
                return self

            async def astream(self, messages):
                from langchain_core.messages import AIMessageChunk

                yield AIMessageChunk(content="半截内容")
                raise RuntimeError("流中断")

        async def scenario():
            gen = run_generation(_MidFailModel(), [], None, "系统提示", [], "你好", max_retries=2)
            events = [e async for e in gen]
            assert [e.type for e in events] == ["text", "error"]
            assert "模型调用失败" in events[-1].data["detail"]

        asyncio.run(scenario())
