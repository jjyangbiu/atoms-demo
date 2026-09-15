"""迭代日志端到端测试（Layer 5：迭代可靠性多层防线；工单 0026 按「改动」累积）。

验收要点：
- 每轮成功生成向 Project.iteration_log 追加一条
  {seq, user_text, files, verdict, mismatch_kind}——seq 语义是「第 N 次改动」，
  user_text 完整落库（原话是权威数据，蒸馏版可能失真），核验结论入账
- 注入模型上下文的渲染文案同步改名（「第 N 次改动」），截断只发生在渲染侧
- 存量项目的旧结构条目（旧序号字段名 round、无核验结论字段）读取时兼容渲染，
  不报错、不丢条目
- 迭代日志注入后续轮的系统提示，且不受对话窗口截断影响（弥补失忆，失败模式 c）
- 失配轮虽不留档快照，日志必须入账（磁盘为什么是现在这个样子，后续轮次要能读懂）
- 存量库（无该列）经 ensure_schema 自动补列，旧行读回空数组
任何测试不得调用真实 MiniMax API。
"""

from conftest import EDIT_STEPS, _turn_result_step, seed_project_files, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project

from app.models import Project


def _load_log(app, project_id) -> list:
    """从库里读回项目的迭代日志。"""
    with app.state.session_factory() as session:
        return list(session.get(Project, project_id).iteration_log)


def _seed_log(app, project_id, entries: list) -> None:
    """预置迭代日志条目（模拟存量项目的库内状态；无写入 API，同 seed_project_files 房规）。"""
    with app.state.session_factory() as session:
        row = session.get(Project, project_id)
        row.iteration_log = entries
        session.commit()


class TestIterationLog:
    def test_appends_one_entry_per_successful_round(self, app, settings, client, auth_headers):
        use_fake_model(
            app,
            [
                # 第一轮迭代：先读后改 index.html（read-before-write 强制）
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
                    ]
                },
                _turn_result_step(summary="第一轮完成。", changed_files=["index.html"]),
                # 第二轮迭代：先读后改 index.html
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v2", "new_text": "v3"})
                    ]
                },
                _turn_result_step(summary="第二轮完成。", changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "第一次修改")
        _stream_messages(client, auth_headers, project["id"], "第二次修改")

        log = _load_log(app, project["id"])
        assert [e["seq"] for e in log] == [1, 2]
        assert [e["user_text"] for e in log] == ["第一次修改", "第二次修改"]
        # 两轮都只改动了 index.html
        assert all(e["files"] == ["index.html"] for e in log)
        # 核验结论入账（工单 0026）：两轮申报与磁盘一致
        assert all(e["verdict"] == "consistent" for e in log)
        assert all(e["mismatch_kind"] is None for e in log)

    def test_injected_into_next_round_system_prompt(self, app, settings, client, auth_headers):
        model = use_fake_model(
            app,
            [
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
                    ]
                },
                _turn_result_step(summary="第一轮完成。", changed_files=["index.html"]),
                # 第二轮申报零改动、磁盘确实无改动：自洽的合法结局
                _turn_result_step(
                    intent="no_change",
                    summary="第二轮无改动。",
                    no_change_reason="本轮无需改动文件。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "第一次修改")
        _stream_messages(client, auth_headers, project["id"], "第二次修改")

        # 第二轮生成的系统提示（received_messages 末次调用首条）应含第一轮迭代日志
        last_system_prompt = model.received_messages[-1][0].content
        assert "迭代日志" in last_system_prompt
        # 编号语义为「第 N 次改动」（工单 0026）：咨询轮不入账后与对话轮错位，
        # 继续叫「第 N 轮」等于对模型说谎
        assert "第1次改动" in last_system_prompt
        assert "第一次修改" in last_system_prompt
        assert "index.html" in last_system_prompt

    def test_survives_history_window_truncation(self, app, settings, client, auth_headers):
        settings.agent_history_window = 1  # 仅保留最近一轮问答
        model = use_fake_model(
            app,
            [
                _turn_result_step(
                    intent="no_change",
                    summary=f"ok{i}",
                    no_change_reason="本轮无需改动文件。",
                )
                for i in range(3)
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "第一条指令")
        _stream_messages(client, auth_headers, project["id"], "第二条指令")
        _stream_messages(client, auth_headers, project["id"], "第三条指令")

        # 迭代日志不截断：三轮全在（本轮均无文件改动）
        log = _load_log(app, project["id"])
        assert [e["seq"] for e in log] == [1, 2, 3]
        assert [e["user_text"] for e in log] == ["第一条指令", "第二条指令", "第三条指令"]
        assert all(e["files"] == [] for e in log)

        # 被对话窗口截掉的早期指令，仍经迭代日志进入第三轮系统提示（弥补失忆）
        last_system_prompt = model.received_messages[-1][0].content
        assert "第一条指令" in last_system_prompt
        assert "第二条指令" in last_system_prompt

    def test_legacy_entries_render_compatibly_mixed_with_new(
        self, app, settings, client, auth_headers
    ):
        """存量旧结构条目（旧序号字段名 round、无核验结论字段）：读取时兼容渲染，
        不报错、不丢条目；与新结构条目混合注入，序号从旧条目计数续编。"""
        _seed_log_entry = {"round": 1, "user_text": "旧结构条目", "files": ["index.html"]}
        model = use_fake_model(
            app,
            [
                # 第一轮新改动：一次真实成功的编辑（共享脚本步房规）
                *EDIT_STEPS,
                _turn_result_step(summary="第一轮完成。", changed_files=["index.html"]),
                # 第二轮：只需出口步，用于捕获混合渲染的系统提示
                _turn_result_step(
                    intent="no_change",
                    summary="无需改动。",
                    no_change_reason="目标状态已存在。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _seed_log(app, project["id"], [dict(_seed_log_entry)])
        _stream_messages(client, auth_headers, project["id"], "第一次修改")
        _stream_messages(client, auth_headers, project["id"], "第二次修改")

        # 第二轮系统提示：新旧条目混合渲染，旧条目不因缺字段报错、不丢
        prompt = model.received_messages[-1][0].content
        legacy_line = next(line for line in prompt.splitlines() if "旧结构条目" in line)
        assert "第1次改动" in legacy_line  # 旧序号字段 round 兼容渲染为改动序号
        assert "核验结论" not in legacy_line  # 无核验结论字段 → 整段省略
        new_line = next(line for line in prompt.splitlines() if "第一次修改" in line)
        assert "第2次改动" in new_line  # 新条目序号从旧条目计数续编
        assert "核验结论" in new_line

        # 落库：旧条目原样保留（读取侧兼容，不做写迁移），新条目按新结构追加
        log = _load_log(app, project["id"])
        assert log[0] == _seed_log_entry
        assert [e["seq"] for e in log[1:]] == [2, 3]

    def test_full_user_text_persisted_but_injection_truncated(
        self, app, settings, client, auth_headers
    ):
        """原始用户诉求完整落库（原话才是权威数据，蒸馏版可能失真）；
        注入侧截断——token 花在注入内容上，权威数据不因注入策略丢失。"""
        long_text = "把首页标题换成深色主题并保持足够的对比度，" * 8 + "尾巴标记END"
        assert len(long_text) > 100
        model = use_fake_model(
            app,
            [
                *EDIT_STEPS,
                _turn_result_step(summary="第一轮完成。", changed_files=["index.html"]),
                _turn_result_step(
                    intent="no_change",
                    summary="无需改动。",
                    no_change_reason="目标状态已存在。",
                ),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], long_text)
        _stream_messages(client, auth_headers, project["id"], "第二次修改")

        # 落库：原话逐字完整
        log = _load_log(app, project["id"])
        assert log[0]["user_text"] == long_text

        # 注入：头部进系统提示，超出截断长度的尾部不注入
        prompt = model.received_messages[-1][0].content
        assert "把首页标题换成深色主题" in prompt
        assert "尾巴标记END" not in prompt

    def test_mismatch_verdict_logged_even_without_snapshot(
        self, app, settings, client, auth_headers
    ):
        """失配轮不留档快照、但日志必须入账（工单 0026）：声称有改动而磁盘零改动，
        回喂一次后仍失配 → 无快照；核验结论照记，否则后续轮次无从理解磁盘现状
        （0028 的越界轮「不留档但留日志」走同一条不变量）。"""
        summary = "把标题换成了深色主题。"
        use_fake_model(
            app,
            [
                _turn_result_step(summary=summary, changed_files=["index.html"]),
                # 回喂后仍原样申报 → 失配标注收尾
                _turn_result_step(summary=summary, changed_files=["index.html"]),
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "把标题改成深色主题")

        snaps = client.get(
            f"/api/projects/{project['id']}/snapshots", headers=auth_headers
        ).json()
        assert snaps == []  # 零改动轮不留档（硬闸不变）

        log = _load_log(app, project["id"])
        assert len(log) == 1  # 不留档 ≠ 不入账
        entry = log[0]
        assert entry["seq"] == 1
        assert entry["files"] == []
        assert entry["verdict"] == "mismatch"
        assert entry["mismatch_kind"] == "verbal_completion"

    def test_fallback_verdict_logged(self, app, settings, client, auth_headers):
        """模型未按标准出口收尾、系统按磁盘事实兜底：核验结论 fallback 同样入账。"""
        use_fake_model(
            app, [*EDIT_STEPS, {"text": "标题现在是 v2。"}, {"text": "不调工具。"}]
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "把 v1 改成 v2")

        log = _load_log(app, project["id"])
        assert log[-1]["verdict"] == "fallback"
        assert log[-1]["mismatch_kind"] is None
        assert log[-1]["files"] == ["index.html"]  # 磁盘真实改动照常入账


def test_ensure_schema_backfills_iteration_log_column(tmp_path):
    """存量库（projects 表缺 iteration_log 列）跑 ensure_schema 后自动补列，旧行读回空数组。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.db import ensure_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    # 建一个缺 iteration_log 列的旧 projects 表并插入一行（模拟升级前的存量库）
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE projects ("
                "id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR(64), "
                "mode VARCHAR(16), created_at DATETIME, updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO projects (id, user_id, name, mode) "
                "VALUES (1, 1, '旧项目', 'engineer')"
            )
        )

    ensure_schema(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
    assert "iteration_log" in columns
    # ORM 读回：存量行按默认值补齐为空数组
    with Session(engine) as session:
        assert session.get(Project, 1).iteration_log == []
    engine.dispose()
