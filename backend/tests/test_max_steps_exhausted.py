"""最大步数耗尽的收尾语义测试（诊断工单：频繁修改报「超过最大步数」未形成版本）。

用户实测症状：迭代轮频繁修改，文件已改好（磁盘可用），但循环超过
agent_max_steps（20）以 error 收尾——不建快照、不入迭代日志，
本轮改动「未形成成功版本」，回滚体系里无从恢复。

降级铁律（routers/projects.py 既有注释）：兜底不没收用户的工作成果——
磁盘有改动照样建快照、入迭代日志。散文兜底路径遵守了铁律，
超步路径是唯一未覆盖的既有成果丢失口。

测试用最小预算（agent_max_steps=3）等价复现，任何测试不得调用真实 API。
"""

from pathlib import Path

from conftest import seed_project_files, use_fake_model
from test_generation import _stream_messages
from test_projects import _create_project


def _project_dir(settings, project_id) -> Path:
    return Path(settings.storage_root) / "projects" / str(project_id)


READ_STEP = {"tool_calls": [("read_file", {"path": "index.html"})]}
EDIT_V2_STEP = {
    "tool_calls": [
        ("edit_file", {"path": "index.html", "old_text": "v1", "new_text": "v2"})
    ]
}
EDIT_V3_STEP = {
    "tool_calls": [
        ("edit_file", {"path": "index.html", "old_text": "v2", "new_text": "v3"})
    ]
}


class TestMaxStepsExhausted:
    def test_exhausted_with_real_changes_still_forms_version(self, app, settings, client, auth_headers):
        """超步时磁盘已有真实改动：改动必须形成版本快照并计入迭代日志。

        场景：预算 3 步，模型读了 1 次、改了 1 次（v1 → v2）后又读 1 次即耗尽——
        频繁修改轮次的缩影：文件已改好，循环却没收尾。
        """
        settings.agent_max_steps = 3
        use_fake_model(app, [READ_STEP, EDIT_V2_STEP, READ_STEP, EDIT_V3_STEP])
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"])

        events = _stream_messages(client, auth_headers, project["id"], "把标题改成深色主题")

        # 改动真实存在（用户看到的「可用」）
        assert (_project_dir(settings, project["id"]) / "index.html").read_text(
            encoding="utf-8"
        ) == "v2"
        # 改动必须形成成功版本：快照留档（当前缺陷：超步 error 不建快照）
        resp = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() != [], "超步耗尽但磁盘有真实改动时，不得没收本轮版本"
        # 改动必须计入迭代日志：后续轮次才能理解磁盘现状（当前缺陷：不入账）
        with app.state.session_factory() as session:
            from app.models import Project

            row = session.get(Project, project["id"])
            log = row.iteration_log or []
        assert log != [], "超步耗尽但磁盘有真实改动时，迭代日志不得缺失"
        assert log[-1]["files"] == ["index.html"]
        # 收尾不得是无成果的裸 error：轮次按磁盘事实兜底完成
        assert not any("超过最大步数" in e.get("detail", "") for e in events), (
            "磁盘有真实改动时，超步不得以裸 error 没收成果"
        )

    def test_exhausted_without_changes_keeps_error(self, app, settings, client, auth_headers):
        """超步且磁盘零改动：维持裸 error、不留档——防「零交付版本」假进展。"""
        settings.agent_max_steps = 3
        use_fake_model(app, [READ_STEP, READ_STEP, READ_STEP, READ_STEP])
        project = _create_project(client, auth_headers)
        seed_project_files(app, project["id"])

        events = _stream_messages(client, auth_headers, project["id"], "把标题改成深色主题")

        assert any(
            e["type"] == "error" and "超过最大步数" in e.get("detail", "") for e in events
        )
        resp = client.get(f"/api/projects/{project['id']}/snapshots", headers=auth_headers)
        assert resp.json() == [], "零改动超步不得建快照（无成果可留档）"
        with app.state.session_factory() as session:
            from app.models import Project

            row = session.get(Project, project["id"])
        assert not (row.iteration_log or []), "零改动超步不得入迭代日志"
