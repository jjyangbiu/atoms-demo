"""生成中途页面刷新（客户端断开 SSE）后的状态保留测试。

症状：页面刷新后，正在生成的项目状态丢失，思考过程也没有了。
最小修复语义：刷新即断开，生成照旧中止，但已流出的思考过程原样落库；
且断开后的消息尾不是收尾结论（前端据此识别"上一轮被中断"并提供重试）。
TestClient 不会传递断开，故在 ASGI 层直接送达 http.disconnect 触发真实路径。
"""

import asyncio
import json
import threading

from langchain_core.messages import AIMessageChunk
from sqlalchemy import select

from app.agent.loop import THINK_CLOSE, THINK_OPEN
from app.models import Message
from test_projects import _create_project


class _ThreadGatedStreamModel:
    """前半段流式产出后卡在 threading.Event 闸门上（跨线程安全）。"""

    def __init__(self, first_chunks: list[str], rest_chunks: list[str], gate: threading.Event):
        self.first_chunks = first_chunks
        self.rest_chunks = rest_chunks
        self.gate = gate

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        for piece in self.first_chunks:
            yield AIMessageChunk(content=piece)
        # 超时兜底：断开触发的生成器关闭不能永远卡在闸门上（避免测试死锁）
        await asyncio.to_thread(self.gate.wait, 10)
        for piece in self.rest_chunks:
            yield AIMessageChunk(content=piece)


class _DisconnectHarness:
    """直接驱动 ASGI 应用：收到指定帧数后送达 http.disconnect（模拟刷新）。"""

    def __init__(self, app):
        self.app = app
        self.disconnect_after_frames = 2
        self.body_frames: list[bytes] = []
        self._disconnect = asyncio.Event()
        self._request_sent = False

    async def receive(self):
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        if message["type"] == "http.response.body":
            self.body_frames.append(message.get("body", b""))
            if len(self.body_frames) >= self.disconnect_after_frames:
                self._disconnect.set()

    async def run(self, token: str, project_id: int, content: str):
        self._body = json.dumps({"content": content}).encode("utf-8")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/projects/{project_id}/messages",
            "raw_path": f"/api/projects/{project_id}/messages".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        await self.app(scope, self.receive, self.send)


def _read_messages(app, project_id: int) -> list[tuple[str, str, str]]:
    """直连库读取消息 (role, kind, content)：断线后回看的真实路径。"""
    with app.state.session_factory() as session:
        rows = session.scalars(
            select(Message).where(Message.project_id == project_id).order_by(Message.id)
        )
        return [(m.role, m.kind, m.content) for m in rows]


async def _disconnect_midstream(app, token: str, project_id: int, gate: threading.Event):
    """发起生成并在中途送达断开，等待收尾落库稳定后返回消息行。"""
    harness = _DisconnectHarness(app)
    await harness.run(token, project_id, "做一个页面")
    # 断开确实发生过：至少收到过响应体帧，且应用已随断开退出本次请求
    assert harness.body_frames, "断开前未收到任何流式帧，回路未走到生成中途"
    # 断开后生成中止（最小修复语义）；轮询落库确认不再有新增行，
    # 同一事件循环内等待，避免生成器关闭与闸门互锁
    gate.set()
    deadline = asyncio.get_event_loop().time() + 3
    rows = _read_messages(app, project_id)
    # 等待断开触发的收尾落库完成（行数稳定一拍即认为已定案）
    stable = 0
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        new_rows = await asyncio.to_thread(_read_messages, app, project_id)
        stable = stable + 1 if len(new_rows) == len(rows) else 0
        rows = new_rows
        if stable >= 4:
            break
    return rows


class TestRefreshMidGeneration:
    def test_refresh_midstream_thinking_survives_and_state_detectable(
        self, app, client, auth_headers
    ):
        gate = threading.Event()
        app.state.model_factory = lambda _s: _ThreadGatedStreamModel(
            [THINK_OPEN + "先分析需求。" + THINK_CLOSE + "构建"],
            ["完成。"],
            gate,
        )
        project = _create_project(client, auth_headers)
        token = auth_headers["Authorization"].removeprefix("Bearer ")
        project_id = project["id"]

        rows = asyncio.run(_disconnect_midstream(app, token, project_id, gate))
        kinds = [(kind, content) for _role, kind, content in rows]
        # 已流出的思考过程刷新后应可回看（症状断言）
        assert ("thinking", "先分析需求。") in kinds, f"思考过程丢失: {kinds}"
        # 生成已中止：不应有最终结论；也不应把半截正文伪装成结论
        assert not any(k == "text" and role == "engineer" for role, k, _ in rows), (
            f"中止的生成不应落最终结论: {kinds}"
        )
        # 消息尾不是收尾结论（text/prd），前端据此识别"上一轮被中断"并提供重试入口
        assert kinds[-1][0] in ("thinking", "event"), f"中断状态不可识别: {kinds}"

    def test_refresh_midstream_pm_thinking_survives(self, app, client, auth_headers):
        """团队模式：PM 产 PRD 途中刷新，已流出的思考同样落库且不留半截 PRD。"""
        gate = threading.Event()
        app.state.model_factory = lambda _s: _ThreadGatedStreamModel(
            [THINK_OPEN + "规划 PRD 结构。" + THINK_CLOSE + "# 需求"],
            ["背景"],
            gate,
        )
        project = _create_project(client, auth_headers, mode="team")
        token = auth_headers["Authorization"].removeprefix("Bearer ")

        rows = asyncio.run(_disconnect_midstream(app, token, project["id"], gate))
        kinds = [(kind, content) for _role, kind, content in rows]
        assert any(
            role == "pm" and kind == "thinking" and content == "规划 PRD 结构。"
            for role, kind, content in rows
        ), f"PM 思考过程丢失: {rows}"
        # 中止的 PRD 轮不留半截 PRD（后端分流以无 prd 消息判未产出，重发即重产）
        assert not any(kind == "prd" for kind, _ in kinds), f"不应落半截 PRD: {kinds}"
        assert kinds[-1][0] in ("thinking", "event"), f"中断状态不可识别: {kinds}"
