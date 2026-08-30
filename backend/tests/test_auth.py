"""注册 / 登录 / 登出 / 当前用户 端到端测试。"""

from conftest import login


class TestRegister:
    def test_register_returns_user(self, client):
        resp = client.post(
            "/api/auth/register", json={"username": "alice", "password": "secret123"}
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "alice"
        assert "password_hash" not in body
        assert "password" not in body

    def test_register_duplicate_username_conflict(self, client, registered_user):
        resp = client.post("/api/auth/register", json=registered_user)
        assert resp.status_code == 409
        assert "占用" in resp.json()["detail"]

    def test_register_short_password_rejected(self, client):
        resp = client.post("/api/auth/register", json={"username": "bob", "password": "123"})
        assert resp.status_code == 422


class TestLogin:
    def test_login_success_returns_token(self, client, registered_user):
        token = login(client, registered_user["username"], registered_user["password"])
        assert token

    def test_login_wrong_password_unauthorized(self, client, registered_user):
        resp = client.post(
            "/api/auth/login",
            json={"username": registered_user["username"], "password": "wrong-pass"},
        )
        assert resp.status_code == 401

    def test_login_unknown_user_unauthorized(self, client):
        resp = client.post("/api/auth/login", json={"username": "ghost", "password": "secret123"})
        assert resp.status_code == 401


class TestMe:
    def test_me_returns_current_user(self, client, registered_user, auth_headers):
        resp = client.get("/api/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["username"] == registered_user["username"]

    def test_me_without_token_unauthorized(self, client):
        assert client.get("/api/auth/me").status_code == 401

    def test_me_with_invalid_token_unauthorized(self, client):
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"})
        assert resp.status_code == 401

    def test_expired_token_unauthorized(self, app, client, registered_user):
        # 用过期时间为负的签发逻辑构造过期令牌，模拟 JWT 过期场景
        from app.security import create_access_token

        settings = app.state.settings
        settings.jwt_expires_minutes = -1
        expired = create_access_token(settings, 1, registered_user["username"])
        settings.jwt_expires_minutes = 60
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
        assert resp.status_code == 401


class TestLogout:
    def test_logout_requires_auth(self, client):
        assert client.post("/api/auth/logout").status_code == 401

    def test_logout_ok(self, client, auth_headers):
        resp = client.post("/api/auth/logout", headers=auth_headers)
        assert resp.status_code == 200
