import pytest

from config import get_app_env
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


def test_api_test_page_requires_login(client):
    """接口测试页（D-026 拆分的一级菜单页）未登录重定向"""
    response = client.get("/api-test", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_logged_in_user_can_open_testbench_and_api_test_pages(logged_in_client):
    cases_page = logged_in_client.get("/testbench")
    assert cases_page.status_code == 200
    assert "测试用例集".encode() in cases_page.content

    api_page = logged_in_client.get("/api-test")
    assert api_page.status_code == 200
    assert "接口测试".encode() in api_page.content


def test_verify_password_rejects_null_and_empty_stored_password():
    """存量 NULL/空串一律拒绝：防止空串与空串相等造成无凭据登录"""
    assert AuthService.verify_password(None, "anything") == (False, False)
    assert AuthService.verify_password("", "anything") == (False, False)
    assert AuthService.verify_password("", "") == (False, False)


def test_verify_password_upgrades_plaintext_with_constant_time_compare():
    valid, should_upgrade = AuthService.verify_password("legacy-pass", "legacy-pass")
    assert valid is True and should_upgrade is True

    # 明文分支同样返回 should_upgrade=True（由登录流程决定是否升级），但校验结果为 False
    matched, should_upgrade = AuthService.verify_password("legacy-pass", "wrong")
    assert matched is False and should_upgrade is True


def test_login_rate_limit_blocks_repeated_failures(client, make_user):
    """窗口内连续失败达上限后，即使密码正确也返回 429；其他用户不受影响"""
    make_user("ratelimit_user", "secret123")
    make_user("other_user", "secret123")

    for _ in range(5):
        response = client.post(
            "/login",
            data={"username": "ratelimit_user", "password": "wrong-pass"},
            follow_redirects=False,
        )
        assert response.status_code == 401

    blocked = client.post(
        "/login",
        data={"username": "ratelimit_user", "password": "secret123"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429

    # 限流按 用户名+IP 隔离：其他用户正常登录不受影响
    other = client.post(
        "/login",
        data={"username": "other_user", "password": "secret123"},
        follow_redirects=False,
    )
    assert other.status_code == 303


def test_pages_use_self_hosted_markdown_assets(logged_in_client):
    """markdown 渲染依赖自托管脚本（不依赖无 SRI 的第三方 CDN），页面须引用且资源可取"""
    chat_page = logged_in_client.get("/chat")
    assert chat_page.status_code == 200
    assert "/static/vendor/marked.min.js" in chat_page.text
    assert "/static/vendor/purify.min.js" in chat_page.text
    assert "/static/js/common.js" in chat_page.text

    for asset in (
        "/static/vendor/marked.min.js",
        "/static/vendor/purify.min.js",
        "/static/js/common.js",
        "/static/js/chat.js",
    ):
        assert logged_in_client.get(asset).status_code == 200


# ---------- 注册输入校验（前端 required/6 位只在 UI 生效） ----------


def test_register_rejects_invalid_credentials(client):
    """服务端兜底：空口令账号此前可注册且能直接登录；超 72 字节口令在 bcrypt 5.x 会抛错"""
    bad_inputs = [
        {"username": "", "password": "secret123"},
        {"username": "alice", "password": ""},
        {"username": "alice", "password": "12345"},
        {"username": "alice", "password": "中" * 25},  # 75 字节 > 72
        {"username": "u" * 51, "password": "secret123"},
        {"username": " alice ", "password": "secret123"},
    ]
    for data in bad_inputs:
        response = client.post("/register", data=data, follow_redirects=False)
        assert response.status_code == 400, data
        assert response.json()["detail"]

    # 一条都没落库：这些凭据登录仍然失败（尤其空口令不再能直接登录）
    assert client.post(
        "/login", data={"username": "", "password": ""}, follow_redirects=False
    ).status_code == 401
    assert client.post(
        "/login", data={"username": "alice", "password": ""}, follow_redirects=False
    ).status_code == 401


def test_register_accepts_boundary_credentials(client, db_session):
    """边界内必须放行：50 字符用户名 + 恰好 72 字节（24 个中文字）口令"""
    response = client.post(
        "/register",
        data={"username": "u" * 50, "password": "中" * 24},
        follow_redirects=False,
    )

    assert response.status_code == 303
    user = db_session.query(User).filter(User.username == "u" * 50).first()
    assert user is not None
    assert AuthService.is_hashed_password(user.password)


def test_login_rate_limit_survives_key_table_pressure(client, make_user, monkeypatch):
    """键表被撑满时不得整体清空：被封锁的键必须继续 429（旧实现 clear() 等于放行）"""
    make_user("victim", "secret123")
    for _ in range(5):
        failed = client.post(
            "/login", data={"username": "victim", "password": "wrong"}, follow_redirects=False
        )
        assert failed.status_code == 401
    assert client.post(
        "/login", data={"username": "victim", "password": "secret123"}, follow_redirects=False
    ).status_code == 429

    from api.endpoints import auth as auth_endpoints

    monkeypatch.setattr(auth_endpoints, "_LOGIN_FAIL_MAX_KEYS", 3)
    # 用其他用户名灌满键表：这些键都未达上限，应被优先淘汰
    for name in ("flood1", "flood2", "flood3", "flood4", "flood5"):
        client.post("/login", data={"username": name, "password": "wrong"}, follow_redirects=False)

    blocked = client.post(
        "/login", data={"username": "victim", "password": "secret123"}, follow_redirects=False
    )
    assert blocked.status_code == 429


def test_get_app_env_rejects_unknown_value(monkeypatch):
    """APP_ENV 拼写错误会让生产保护（会话密钥/库口令/cookie 安全标志）静默失效，须 fail-fast"""
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(RuntimeError):
        get_app_env()

    monkeypatch.setenv("APP_ENV", " Production ")
    assert get_app_env() == "production"
