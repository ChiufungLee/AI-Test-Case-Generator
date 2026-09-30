from main import create_app
from models.database import build_database_url
from models.user import User
from services.auth_service import AuthService


def test_register_hashes_password(client, db_session):
    response = client.post(
        "/register",
        data={"username": "new_user", "password": "plain-secret"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    user = db_session.query(User).filter(User.username == "new_user").first()
    assert user is not None
    assert user.password != "plain-secret"
    assert AuthService.is_hashed_password(user.password)



def test_login_sets_session_for_bcrypt_user(client, make_user):
    make_user("bcrypt_user", "secret123", hashed=True)

    response = client.post(
        "/login",
        data={"username": "bcrypt_user", "password": "secret123"},
        follow_redirects=False,
    )

    assert response.status_code == 303

    protected_response = client.get("/api/knowledge-bases/")
    assert protected_response.status_code == 200



def test_login_upgrades_legacy_plaintext_password(client, db_session, make_user):
    user = make_user("legacy_user", "legacy-pass", hashed=False)

    response = client.post(
        "/login",
        data={"username": "legacy_user", "password": "legacy-pass"},
        follow_redirects=False,
    )

    assert response.status_code == 303

    db_session.expire_all()
    refreshed_user = db_session.query(User).filter(User.id == user.id).first()
    assert refreshed_user is not None
    assert refreshed_user.password != "legacy-pass"
    assert AuthService.is_hashed_password(refreshed_user.password)



def test_logout_clears_session(logged_in_client):
    response = logged_in_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303

    protected_response = logged_in_client.get("/api/knowledge-bases/")
    assert protected_response.status_code == 401



def test_knowledge_page_requires_login(client):
    response = client.get("/knowledge", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"



def test_logged_in_user_can_open_knowledge_page(logged_in_client):
    response = logged_in_client.get("/knowledge")
    assert response.status_code == 200



def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200



def test_register_page_renders(client):
    response = client.get("/register")
    assert response.status_code == 200



def test_anonymous_access_to_protected_endpoint_returns_401(client):
    response = client.get("/api/knowledge-bases/")
    assert response.status_code == 401
    assert response.json() == {"detail": "未登录"}



def test_create_app_requires_session_secret_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    try:
        create_app()
    except RuntimeError as exc:
        assert "SESSION_SECRET_KEY" in str(exc)
    else:
        raise AssertionError("create_app() should reject missing production session secret")



def test_build_database_url_requires_non_default_password_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MYSQL_PASSWORD", "password")

    try:
        build_database_url()
    except RuntimeError as exc:
        assert "MYSQL_PASSWORD" in str(exc)
    else:
        raise AssertionError("build_database_url() should reject default production password")



def test_create_user_hides_internal_exception_details(db_session, monkeypatch):
    def broken_commit():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(AuthService, "get_user_by_username", lambda db, username: None)
    monkeypatch.setattr(db_session, "commit", broken_commit)

    result = AuthService.create_user(db_session, "oops_user", "secret123")

    assert result["success"] is False
    assert result["error"] == "创建用户失败，请稍后重试"



def test_history_endpoint_allows_missing_knowledge_base_id(logged_in_client):
    response = logged_in_client.get("/api/history", params={"scenario": "product_manual"})
    assert response.status_code == 200
    assert "groups" in response.json()
