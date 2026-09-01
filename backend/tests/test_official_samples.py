"""官方示例冷启动端到端测试（工单 0012）。

验收要点：
- 画廊条目携带官方示例标记；普通发布默认不带
- 种子函数走完整链路（建项目 → 生成 → 一轮迭代 → 发布 → 标记官方），画廊可见、可打开运行、可克隆
- 种子可重复执行：已灌入的示例不会重复生成（幂等）
- 单个示例生成失败被记录且不阻断后续示例；失败示例不留发布记录
- 存量旧库缺 official 列时种子自动补列（新环境部署后可重建画廊种子）

任何测试不得调用真实 MiniMax API：生成由可编程伪模型驱动。
"""

from sqlalchemy import select, text

from app.models import Publication
from app.official_samples import (
    OFFICIAL_SAMPLE_SPECS,
    OFFICIAL_USERNAME,
    seed_official_samples,
)
from conftest import FIRST_BUILD_CLARIFY_STEP, use_fake_model
from test_world import _register_and_login

# 每个示例消耗一段“澄清收敛 → 确认后首轮生成（写文件→收尾）→ 迭代（局部改→收尾）”
# 脚本，乘上示例数供整轮灌入使用——与灌入实现的对话链路一一对应（工单 0015），
# 澄清与迭代环节同样在回归覆盖内。
_ONE_SAMPLE_SCRIPT = [
    FIRST_BUILD_CLARIFY_STEP,
    {"tool_calls": [("write_file", {"path": "index.html", "content": "<h1>示例</h1>"})]},
    {"text": "已完成。"},
    {"tool_calls": [("edit_file", {"path": "index.html", "old_text": "示例", "new_text": "官方示例"})]},
    {"text": "已调整。"},
]


def _seed_with_fake_model(app, client):
    use_fake_model(app, _ONE_SAMPLE_SCRIPT * len(OFFICIAL_SAMPLE_SPECS))
    return seed_official_samples(app, client)


class TestOfficialFlag:
    def test_regular_publish_is_not_official(self, app, client, auth_headers):
        """普通用户发布的应用在画廊里不带官方标记。"""
        from test_world import _generate_and_publish

        _, pub = _generate_and_publish(app, client, auth_headers)
        entry = client.get(f"/api/world/{pub['slug']}").json()
        assert entry["official"] is False

    def test_world_entries_carry_official_flag(self, app, client):
        results = _seed_with_fake_model(app, client)
        assert [r.status for r in results] == ["seeded"] * len(OFFICIAL_SAMPLE_SPECS)

        apps = client.get("/api/world").json()
        assert len(apps) == len(OFFICIAL_SAMPLE_SPECS)
        assert all(e["official"] for e in apps)
        # 画廊元信息：作者是官方账号，描述取首次诉求；发布页匿名可打开（可运行验收）
        by_title = {e["title"]: e for e in apps}
        for spec in OFFICIAL_SAMPLE_SPECS:
            entry = by_title[spec.name]
            assert entry["author"] == OFFICIAL_USERNAME
            assert entry["description"] == spec.ask[:120]
            assert entry["preview_url"] == f"/p/{entry['slug']}/"
            assert client.get(f"/p/{entry['slug']}").status_code == 200

    def test_official_sample_can_be_cloned(self, app, client):
        _seed_with_fake_model(app, client)
        slug = client.get("/api/world").json()[0]["slug"]
        eve_headers = _register_and_login(client, "eve")

        resp = client.post(f"/api/world/{slug}/clone", headers=eve_headers)
        assert resp.status_code == 201, resp.text
        files = client.get(f"/api/projects/{resp.json()['id']}/files", headers=eve_headers).json()
        assert [f["path"] for f in files] == ["index.html"]


class TestSeedRepeatable:
    def test_rerun_is_idempotent(self, app, client):
        first = _seed_with_fake_model(app, client)
        assert [r.status for r in first] == ["seeded"] * len(OFFICIAL_SAMPLE_SPECS)

        # 重复执行：全部跳过且不重新生成（未配伪模型，若走生成链路会脚本耗尽报错），
        # 画廊条目数与稳定链接均不变——幂等性由 HTTP 可观察状态断言。
        second = seed_official_samples(app, client)
        assert [r.status for r in second] == ["skipped"] * len(OFFICIAL_SAMPLE_SPECS)
        assert [r.slug for r in second] == [r.slug for r in first]
        assert len(client.get("/api/world").json()) == len(OFFICIAL_SAMPLE_SPECS)

    def test_rerun_recovers_unpublished_partial(self, app, client):
        """上次生成成功但没走到发布的项目：重跑补齐发布与标记，而不是重复生成。"""
        use_fake_model(app, _ONE_SAMPLE_SCRIPT)
        results = seed_official_samples(app, client)
        assert results[0].status == "seeded"

        # 模拟第一个示例在发布前中断：撤掉发布记录（仅此处触达 DB：构造中断现场）
        session_factory = app.state.session_factory
        with session_factory() as session:
            publication = session.scalar(select(Publication))
            slug = publication.slug
            session.delete(publication)
            session.commit()

        use_fake_model(app, _ONE_SAMPLE_SCRIPT * (len(OFFICIAL_SAMPLE_SPECS) - 1))
        results = seed_official_samples(app, client)
        assert results[0].status == "seeded"  # 恢复而非跳过
        assert [r.status for r in results[1:]] == ["seeded"] * (len(OFFICIAL_SAMPLE_SPECS) - 1)
        # 补齐发布后重新获得稳定链接（原链接已随下架失效），画廊标记为官方示例
        entry = client.get(f"/api/world/{results[0].slug}").json()
        assert entry["official"] is True
        assert client.get(f"/api/world/{slug}").status_code == 404


class TestSeedFailures:
    def test_generation_failure_recorded_and_others_continue(self, app, client):
        # 第一个示例在澄清阶段耗尽重试后报错；其余示例照常（工单 0015）
        script = [RuntimeError] * 3  # 默认 agent_max_retries=2 → 3 次调用全失败
        script += _ONE_SAMPLE_SCRIPT * (len(OFFICIAL_SAMPLE_SPECS) - 1)
        use_fake_model(app, script)
        results = seed_official_samples(app, client)

        assert results[0].status == "failed"
        assert results[0].error
        assert [r.status for r in results[1:]] == ["seeded"] * (len(OFFICIAL_SAMPLE_SPECS) - 1)

        # 失败示例不留发布记录，画廊只有成功的
        apps = client.get("/api/world").json()
        assert len(apps) == len(OFFICIAL_SAMPLE_SPECS) - 1
        assert OFFICIAL_SAMPLE_SPECS[0].name not in {e["title"] for e in apps}

    def test_official_column_added_to_legacy_db(self, app, client):
        """旧库的 publications 表没有 official 列：种子自动补列后照常灌入。"""
        # 以不含 official 列的 DDL 重建 publications，模拟新代码遇到旧库
        with app.state.engine.begin() as conn:
            conn.execute(text("DROP TABLE publications"))
            conn.execute(
                text(
                    """
                    CREATE TABLE publications (
                        id INTEGER PRIMARY KEY,
                        project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id),
                        slug VARCHAR(32) NOT NULL UNIQUE,
                        created_at DATETIME
                    )
                    """
                )
            )

        results = _seed_with_fake_model(app, client)
        assert [r.status for r in results] == ["seeded"] * len(OFFICIAL_SAMPLE_SPECS)
        assert all(e["official"] for e in client.get("/api/world").json())
