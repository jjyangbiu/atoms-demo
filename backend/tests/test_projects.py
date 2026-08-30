"""项目 CRUD 与对话历史端到端测试。"""

from conftest import login


def _create_project(client, headers, name="番茄钟", mode="engineer"):
    resp = client.post("/api/projects", json={"name": name, "mode": mode}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestProjectCrud:
    def test_create_and_list(self, client, auth_headers):
        project = _create_project(client, auth_headers)
        assert project["mode"] == "engineer"
        resp = client.get("/api/projects", headers=auth_headers)
        assert resp.status_code == 200
        assert [p["id"] for p in resp.json()] == [project["id"]]

    def test_get_detail(self, client, auth_headers):
        project = _create_project(client, auth_headers)
        resp = client.get(f"/api/projects/{project['id']}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "番茄钟"

    def test_delete_removes_project(self, client, auth_headers):
        project = _create_project(client, auth_headers)
        assert client.delete(f"/api/projects/{project['id']}", headers=auth_headers).status_code == 204
        assert client.get(f"/api/projects/{project['id']}", headers=auth_headers).status_code == 404

    def test_requires_login(self, client):
        assert client.post("/api/projects", json={"name": "x"}).status_code == 401
        assert client.get("/api/projects").status_code == 401

    def test_other_user_project_is_404(self, app, client, auth_headers):
        project = _create_project(client, auth_headers)
        # 第二个用户看不到第一个用户的项目（数据隔离，用户故事 38）
        client.post("/api/auth/register", json={"username": "mallory", "password": "secret123"})
        other_token = login(client, "mallory", "secret123")
        other_headers = {"Authorization": f"Bearer {other_token}"}
        assert client.get(f"/api/projects/{project['id']}", headers=other_headers).status_code == 404
        assert (
            client.delete(f"/api/projects/{project['id']}", headers=other_headers).status_code == 404
        )

    def test_invalid_mode_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/projects", json={"name": "x", "mode": "hacker"}, headers=auth_headers
        )
        assert resp.status_code == 422


class TestMessageHistory:
    def test_empty_history(self, client, auth_headers):
        project = _create_project(client, auth_headers)
        resp = client.get(f"/api/projects/{project['id']}/messages", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []
