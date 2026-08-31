"""生成限流端到端测试（工单 0011）。

验收要点：
- 超出每用户每小时上限返回 429，响应携带建议重试等待信息（结构化体 + Retry-After 头）
- 全局并发占满时新生成被拒，不影响进行中的生成；结束后名额释放
- 引导类响应不调模型不计限额；确认 PRD 触发的生成同样受限流约束
- 限额参数经配置（环境变量）注入，测试以可控时钟验证滑动窗口边界
任何测试不得调用真实 MiniMax API。
"""

import asyncio
import json
import threading

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from conftest import use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project


class FakeClock:
    """可控时钟：注入限流器以验证滑动窗口边界。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _use_clock(app) -> FakeClock:
    clock = FakeClock()
    app.state.rate_limiter.clock = clock
    return clock


class GatedModel:
    """把生成挂在「进行中」直到放行：用于构造全局并发占满的场景。

    满足 loop.run_generation 的最小约定（bind_tools / ainvoke），
    第一步写一个文件，第二步以纯文本收尾。
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "index.html", "content": "hi"},
                        "id": "call_0",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="完成。")


def _parse_sse(resp) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in resp.iter_lines()
        if line.startswith("data: ")
    ]


class TestUserHourlyLimit:
    def test_exceed_quota_returns_429_with_retry_info(self, app, client, auth_headers):
        _use_clock(app)
        app.state.rate_limiter.per_user_hourly = 2
        use_fake_model(app, [{"text": "完成"}] * 3)
        project = _create_project(client, auth_headers)

        _stream_messages(client, auth_headers, project["id"], "第一次")
        _stream_messages(client, auth_headers, project["id"], "第二次")

        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "第三次"},
            headers=auth_headers,
        )
        assert resp.status_code == 429
        detail = resp.json()["detail"]
        assert detail["error"] == "rate_limited"
        assert detail["reason"] == "user_hourly"
        assert 0 < detail["retry_after"] <= 3600
        assert "重试" in detail["message"]
        assert int(resp.headers["Retry-After"]) == detail["retry_after"]

        # 被拒请求不落用户消息（两次成功生成的两条仍在）
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert len([m for m in messages if m["role"] == "user"]) == 2

    def test_window_slides_with_controllable_clock(self, app, client, auth_headers):
        clock = _use_clock(app)
        app.state.rate_limiter.per_user_hourly = 1
        use_fake_model(app, [{"text": "完成"}] * 3)
        project = _create_project(client, auth_headers)

        _stream_messages(client, auth_headers, project["id"], "第一次")

        # 窗口内第二次被拒
        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "太早"},
            headers=auth_headers,
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["retry_after"] > 0

        # 差一秒到窗口边界仍被拒；跨过边界后放行（滑动窗口边界）
        clock.advance(3599)
        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "还差一秒"},
            headers=auth_headers,
        )
        assert resp.status_code == 429

        clock.advance(2)
        events = _stream_messages(client, auth_headers, project["id"], "过界重试")
        assert events[-1]["type"] == "done"

    def test_rejected_request_does_not_consume_quota(self, app, client, auth_headers):
        clock = _use_clock(app)
        app.state.rate_limiter.per_user_hourly = 1
        use_fake_model(app, [{"text": "完成"}] * 2)
        project = _create_project(client, auth_headers)

        _stream_messages(client, auth_headers, project["id"], "第一次")
        for _ in range(3):
            resp = client.post(
                f"/api/projects/{project['id']}/messages",
                json={"content": "被拒"},
                headers=auth_headers,
            )
            assert resp.status_code == 429

        # 多次被拒不额外占用窗口：跨过边界后仍只需要等最早那次过期
        clock.advance(3601)
        events = _stream_messages(client, auth_headers, project["id"], "恢复")
        assert events[-1]["type"] == "done"


class TestTeamModeLimits:
    def test_guide_not_counted_but_confirm_respects_limit(self, app, client, auth_headers):
        clock = _use_clock(app)
        app.state.rate_limiter.per_user_hourly = 1
        prd_script = [
            {"text": "# PRD\n\n做一个番茄钟。"},
            {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>番茄钟</h1>"})]},
            {"text": "实现完成。"},
        ]
        use_fake_model(app, prd_script)
        project = _create_project(client, auth_headers, mode="team")

        # PM 产 PRD 消耗唯一名额
        events = _stream_messages(client, auth_headers, project["id"], "做一个番茄钟")
        assert events[-1]["type"] == "done"

        # 待确认时发普通消息走引导：不调模型、不计限额，不受限流
        resp = client.post(
            f"/api/projects/{project['id']}/messages",
            json={"content": "催一下"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # 确认会触发工程师生成：限额已用完，429 且不落确认消息
        resp = client.post(
            f"/api/projects/{project['id']}/prd/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["reason"] == "user_hourly"
        messages = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers).json()
        assert not any(m["kind"] == "prd_confirm" for m in messages)

        # 窗口过期后确认放行，工程师生成正常完成
        clock.advance(3601)
        with client.stream(
            "POST",
            f"/api/projects/{project['id']}/prd/confirm",
            json={"feedback": ""},
            headers=auth_headers,
        ) as resp:
            assert resp.status_code == 200
            events = _parse_sse(resp)
        assert events[-1]["type"] == "done"


class TestGlobalConcurrencyLimit:
    def test_full_capacity_rejects_second_without_hurting_inflight(self, app, client, auth_headers):
        app.state.rate_limiter.max_concurrent = 1
        model = GatedModel()
        app.state.model_factory = lambda settings: model
        p1 = _create_project(client, auth_headers)
        p2 = _create_project(client, auth_headers)

        result: dict = {}

        def busy_generation():
            busy_client = TestClient(app)
            with busy_client.stream(
                "POST",
                f"/api/projects/{p1['id']}/messages",
                json={"content": "慢生成"},
                headers=auth_headers,
            ) as resp:
                result["status"] = resp.status_code
                result["events"] = _parse_sse(resp)

        worker = threading.Thread(target=busy_generation)
        worker.start()
        assert model.entered.wait(5), "首个生成应已进入模型调用（占用全局名额）"

        # 名额占满：第二个项目的生成被拒
        second = client.post(
            f"/api/projects/{p2['id']}/messages",
            json={"content": "第二个"},
            headers=auth_headers,
        )
        assert second.status_code == 429
        detail = second.json()["detail"]
        assert detail["reason"] == "global_concurrency"
        assert detail["retry_after"] > 0
        assert "重试" in detail["message"]
        assert int(second.headers["Retry-After"]) == detail["retry_after"]

        # 放行后：进行中的生成不受影响，正常走到 done
        model.release.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert result["status"] == 200
        assert result["events"][-1]["type"] == "done"

        # 名额已释放：新生成恢复正常
        events = _stream_messages(client, auth_headers, p2["id"], "重试")
        assert events[-1]["type"] == "done"


class TestLimitConfiguration:
    def test_limits_wired_from_settings(self, monkeypatch, tmp_path):
        """限额经环境变量（ATOMS_ 前缀）注入，装配进应用限流器。"""
        monkeypatch.setenv("ATOMS_RATE_LIMIT_PER_USER_HOURLY", "7")
        monkeypatch.setenv("ATOMS_RATE_LIMIT_MAX_CONCURRENT", "3")
        from app.config import Settings
        from app.main import create_app

        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'conf.db'}",
            storage_root=str(tmp_path / "storage"),
            milvus_uri=str(tmp_path / "milvus" / "atoms.db"),
            _env_file=None,
        )
        assert settings.rate_limit_per_user_hourly == 7
        assert settings.rate_limit_max_concurrent == 3
        app = create_app(settings)
        assert app.state.rate_limiter.per_user_hourly == 7
        assert app.state.rate_limiter.max_concurrent == 3

    def test_zero_disables_limits(self, app, client, auth_headers):
        app.state.rate_limiter.per_user_hourly = 0
        app.state.rate_limiter.max_concurrent = 0
        _use_clock(app)
        use_fake_model(app, [{"text": "完成"}] * 3)
        project = _create_project(client, auth_headers)
        for i in range(3):
            events = _stream_messages(client, auth_headers, project["id"], f"第 {i} 次")
            assert events[-1]["type"] == "done"
