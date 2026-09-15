"""迭代日志端到端测试（Layer 5：迭代可靠性多层防线）。

验收要点：
- 每轮成功生成向 Project.iteration_log 追加一条 {round, user_text, files}
- 迭代日志注入后续轮的系统提示，且不受对话窗口截断影响（弥补失忆，失败模式 c）
- 存量库（无该列）经 ensure_schema 自动补列，旧行读回空数组
任何测试不得调用真实 MiniMax API。
"""

from conftest import seed_project_files, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project

from app.models import Project


def _load_log(app, project_id) -> list:
    """从库里读回项目的迭代日志。"""
    with app.state.session_factory() as session:
        return list(session.get(Project, project_id).iteration_log)


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
                {"text": "第一轮完成。"},
                # 第二轮迭代：先读后改 index.html
                {"tool_calls": [("read_file", {"path": "index.html"})]},
                {
                    "tool_calls": [
                        ("edit_file", {"path": "index.html", "old_text": "v2", "new_text": "v3"})
                    ]
                },
                {"text": "第二轮完成。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "第一次修改")
        _stream_messages(client, auth_headers, project["id"], "第二次修改")

        log = _load_log(app, project["id"])
        assert [e["round"] for e in log] == [1, 2]
        assert [e["user_text"] for e in log] == ["第一次修改", "第二次修改"]
        # 两轮都只改动了 index.html
        assert all(e["files"] == ["index.html"] for e in log)

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
                {"text": "第一轮完成。"},
                # 第二轮纯文本、无文件改动
                {"text": "第二轮无改动。"},
            ],
        )
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "第一次修改")
        _stream_messages(client, auth_headers, project["id"], "第二次修改")

        # 第二轮生成的系统提示（received_messages 末次调用首条）应含第一轮迭代日志
        last_system_prompt = model.received_messages[-1][0].content
        assert "迭代日志" in last_system_prompt
        assert "第一次修改" in last_system_prompt
        assert "index.html" in last_system_prompt

    def test_survives_history_window_truncation(self, app, settings, client, auth_headers):
        settings.agent_history_window = 1  # 仅保留最近一轮问答
        model = use_fake_model(app, [{"text": f"ok{i}"} for i in range(3)])
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"], {"index.html": "v1"})
        _stream_messages(client, auth_headers, project["id"], "第一条指令")
        _stream_messages(client, auth_headers, project["id"], "第二条指令")
        _stream_messages(client, auth_headers, project["id"], "第三条指令")

        # 迭代日志不截断：三轮全在（本轮均无文件改动）
        log = _load_log(app, project["id"])
        assert [e["round"] for e in log] == [1, 2, 3]
        assert [e["user_text"] for e in log] == ["第一条指令", "第二条指令", "第三条指令"]
        assert all(e["files"] == [] for e in log)

        # 被对话窗口截掉的早期指令，仍经迭代日志进入第三轮系统提示（弥补失忆）
        last_system_prompt = model.received_messages[-1][0].content
        assert "第一条指令" in last_system_prompt
        assert "第二条指令" in last_system_prompt


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
