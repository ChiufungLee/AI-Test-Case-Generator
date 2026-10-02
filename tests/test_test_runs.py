"""测试执行：占用语义、httpx 执行器（MockTransport 桩）、结果落库、SSE 事件流、权限"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.endpoints.run_hub import RunHub
from conftest import parse_sse_events
from models.api_test_models import TestRun
from models.user import User
from services import api_spec_service, test_run_service
from services.test_run_service import NotFoundError


@pytest.fixture()
def alice(db_session, make_user):
    make_user("alice", "secret123")
    return db_session.query(User).filter(User.username == "alice").first().id


@pytest.fixture()
def bob(db_session, make_user):
    make_user("bob", "secret123")
    return db_session.query(User).filter(User.username == "bob").first().id


def _alice_id(db_session) -> int:
    return db_session.query(User).filter(User.username == "alice").first().id


def _bob_id(db_session) -> int:
    return db_session.query(User).filter(User.username == "bob").first().id


def _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, user_id, expected_status=200):
    spec = make_api_spec(user_id, name="目标服务", endpoint_count=1)
    endpoint = make_api_endpoint(spec.id, method="get", path="/ping")
    make_api_endpoint_case(
        endpoint.id, name="正常请求",
        request_json=json.dumps({"query": {}}),
        expected_status=expected_status,
    )
    return spec, endpoint


# ---------- 占用语义（乐观锁/冲突） ----------


def test_try_claim_run_creates_and_blocks_second(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice):
    spec, endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)

    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    assert run is not None
    assert run.status == "running"
    assert run.total == 1
    # claim 时快照本轮目标接口（PR4-UX 反馈 4：执行历史展示接口）
    assert json.loads(run.endpoints_json) == ["GET /ping"]

    # 本人再次占用 → None（调用方转订阅既有运行）
    assert test_run_service.try_claim_run(spec.id, alice, "http://target.example") is None


def test_try_claim_run_spec_owner_only(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, bob):
    """执行为 owner-only：非 owner 一律 404 语义（不暴露存在性；ConflictError 仅为纵深防御）"""
    spec, endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)
    with pytest.raises(NotFoundError):
        test_run_service.try_claim_run(spec.id, bob, "http://target.example")


def test_try_claim_run_validates_base_url(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice):
    spec, endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)
    with pytest.raises(ValueError):
        test_run_service.try_claim_run(spec.id, alice, "ftp://target.example")


def test_try_claim_run_spec_owner_only(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, bob):
    spec, endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)
    with pytest.raises(NotFoundError):
        test_run_service.try_claim_run(spec.id, bob, "http://target.example")  # 非 owner → 404 语义


# ---------- 执行器（MockTransport 桩：三态） ----------


class _RecordingHub(RunHub):
    """记录事件的测试桩：execute_run 经 publish_key 发布，收尾后 handle 已注销但仍可读取缓冲"""


@pytest.mark.asyncio
async def test_execute_run_passes_and_persists(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    spec, endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    # 结果落库
    view = test_run_service.get_run_view(run.id, alice)
    assert view["status"] == "completed"
    assert view["passed"] == 1 and view["failed"] == 0 and view["errored"] == 0
    assert view["results"][0]["verdict"] == "passed"
    assert calls == ["http://target.example/ping"]

    # 事件序列（run_started → case_done → completed），缓冲在 finish 后仍可读
    assert [e["event"] for e in handle.events] == ["run_started", "case_done", "completed"]
    assert handle.events[0]["endpoints"] == ["GET /ping"]
    assert handle.events[1]["verdict"] == "passed"
    # case_done 事件带失败原因与响应快照（PR4-UX 反馈 3：页面展示失败原因）
    assert handle.events[1]["failure_reason"] is None
    assert handle.events[1]["response"]["status"] == 200
    assert handle.events[1]["response"]["body"]


@pytest.mark.asyncio
async def test_execute_run_timeout_and_mismatch(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    spec = make_api_spec(alice, name="目标服务", endpoint_count=2)
    slow = make_api_endpoint(spec.id, method="get", path="/slow")
    make_api_endpoint_case(slow.id, name="慢接口", request_json="{}", expected_status=200)
    code = make_api_endpoint(spec.id, method="get", path="/code")
    make_api_endpoint_case(code.id, name="状态不符", request_json="{}", expected_status=201)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slow":
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")

    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    view = test_run_service.get_run_view(run.id, alice)
    assert view["errored"] == 1 and view["failed"] == 1 and view["passed"] == 0
    error_result = next(r for r in view["results"] if r["verdict"] == "error")
    assert error_result["failure_reason"]
    failed_result = next(r for r in view["results"] if r["verdict"] == "failed")
    assert "预期 201" in failed_result["failure_reason"]
    assert failed_result["actual_status"] == 500


@pytest.mark.asyncio
async def test_execute_run_request_snapshot(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """path 参数代入模板 + 请求快照（url/query/body）"""
    spec = make_api_spec(alice, name="目标服务", endpoint_count=1)
    endpoint = make_api_endpoint(spec.id, method="post", path="/users/{id}")
    make_api_endpoint_case(
        endpoint.id, name="正常请求",
        request_json=json.dumps({"path": {"id": 7}, "query": {"verbose": True}, "body": {"username": "abc"}}),
        expected_status=201,
    )

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(201, json={"id": 7})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")

    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    assert calls == ["http://target.example/users/7?verbose=true"]
    view = test_run_service.get_run_view(run.id, alice)
    request = view["results"][0]["request"]
    assert request["method"] == "POST"
    # 快照 url 仅含路径解析结果；query 单独记录在 request["query"]
    assert request["url"] == "http://target.example/users/7"
    assert request["body"] == {"username": "abc"}
    assert request["query"] == {"verbose": True}
    assert view["results"][0]["verdict"] == "passed"


@pytest.mark.asyncio
async def test_execute_run_missing_run_is_noop():
    hub = RunHub()
    await test_run_service.execute_run("nonexistent", "http://target.example", None, hub)
    assert hub.get("nonexistent") is None


# ---------- 端点 ----------


def test_run_endpoints_require_login(client, db_session, make_user, make_api_spec, make_api_endpoint, make_api_endpoint_case):
    make_user("alice", "secret123")
    spec = make_api_spec(db_session.query(User).filter(User.username == "alice").first().id)
    endpoint = make_api_endpoint(spec.id)
    make_api_endpoint_case(endpoint.id, name="正常请求", request_json="{}", expected_status=200)

    assert client.post(f"/api/api-specs/{spec.id}/runs", json={"base_url": "http://t.example"}).status_code == 401
    assert client.get("/api/test-runs/x").status_code == 401
    assert client.get(f"/api/api-specs/{spec.id}/runs").status_code == 401


def test_run_endpoint_streams_and_persists(
    logged_in_client, db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, monkeypatch
):
    spec, endpoint = _spec_with_case(
        db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, _alice_id(db_session)
    )
    monkeypatch.setattr(
        test_run_service, "_create_run_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        ),
    )

    response = logged_in_client.post(f"/api/api-specs/{spec.id}/runs", json={"base_url": "http://target.example"})
    assert response.status_code == 200
    events = parse_sse_events(response.text)
    assert [e["event"] for e in events] == ["run_started", "case_done", "completed"]
    run_id = events[0]["run_id"]

    view = logged_in_client.get(f"/api/test-runs/{run_id}")
    assert view.status_code == 200
    assert view.json()["status"] == "completed"
    assert view.json()["passed"] == 1

    history = logged_in_client.get(f"/api/api-specs/{spec.id}/runs")
    assert len(history.json()) == 1


def test_run_endpoint_stale_running_conflicts(
    logged_in_client, db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case
):
    """进程重启残留的 running（进程内无对应运行）→ 409 提示"""
    spec, endpoint = _spec_with_case(
        db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, _alice_id(db_session)
    )
    db_session.add(TestRun(
        spec_id=spec.id, base_url="http://old.example",
        status="running", total=1, created_by=_alice_id(db_session),
    ))
    db_session.commit()

    response = logged_in_client.post(f"/api/api-specs/{spec.id}/runs", json={"base_url": "http://target.example"})
    assert response.status_code == 409


def test_run_endpoint_invalid_base_url(
    logged_in_client, db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case
):
    spec, endpoint = _spec_with_case(
        db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, _alice_id(db_session)
    )
    response = logged_in_client.post(f"/api/api-specs/{spec.id}/runs", json={"base_url": "ftp://target.example"})
    assert response.status_code == 422


def test_run_endpoint_requires_spec_ownership(logged_in_client, db_session, make_user, make_api_spec, make_api_endpoint, make_api_endpoint_case):
    make_user("bob", "secret123")
    spec = make_api_spec(_bob_id(db_session), name="他人规格", visibility="shared")
    endpoint = make_api_endpoint(spec.id)
    make_api_endpoint_case(endpoint.id, name="正常请求", request_json="{}", expected_status=200)

    # 共享读者不可发起执行（owner-only）
    response = logged_in_client.post(f"/api/api-specs/{spec.id}/runs", json={"base_url": "http://t.example"})
    assert response.status_code == 404

# ---------- 表单请求体执行（D-024：按媒体类型发送） ----------


@pytest.mark.asyncio
async def test_execute_run_sends_urlencoded_form(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """urlencoded 接口：请求体按表单发送而非 JSON（此前这类接口的正常请求必 422）"""
    spec = make_api_spec(alice, name="登录服务", endpoint_count=1)
    endpoint = make_api_endpoint(
        spec.id, method="post", path="/login",
        request_body_json=json.dumps({
            "type": "object", "required": ["username", "password"],
            "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
        }),
        request_body_media_type="application/x-www-form-urlencoded",
    )
    make_api_endpoint_case(
        endpoint.id, name="正常请求",
        request_json=json.dumps({"body": {"username": "alice", "password": "secret"}}),
        expected_status=200,
    )

    captures = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captures["content_type"] = request.headers.get("content-type")
        captures["content"] = request.content
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    assert captures["content_type"] == "application/x-www-form-urlencoded"
    assert b"username=alice" in captures["content"]
    view = test_run_service.get_run_view(run.id, alice)
    assert view["passed"] == 1
    assert view["results"][0]["request"]["media_type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_execute_run_sends_multipart_with_placeholder_file(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """multipart 接口：binary 字段转占位文件，普通字段保留在表单中"""
    spec = make_api_spec(alice, name="上传服务", endpoint_count=1)
    endpoint = make_api_endpoint(
        spec.id, method="post", path="/upload",
        request_body_json=json.dumps({
            "type": "object", "required": ["file", "note"],
            "properties": {"file": {"type": "string", "format": "binary"}, "note": {"type": "string"}},
        }),
        request_body_media_type="multipart/form-data",
    )
    make_api_endpoint_case(
        endpoint.id, name="正常请求",
        request_json=json.dumps({"body": {"file": "test-file.bin", "note": "hello"}}),
        expected_status=200,
    )

    captures = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captures["content_type"] = request.headers.get("content-type", "")
        captures["content"] = request.content
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    assert captures["content_type"].startswith("multipart/form-data")
    assert b'name="file"' in captures["content"]
    assert b"test-file-content" in captures["content"]
    assert b'name="note"' in captures["content"]
    assert b"hello" in captures["content"]
    view = test_run_service.get_run_view(run.id, alice)
    assert view["passed"] == 1


# ---------- 登录态前置请求（D-025） ----------


@pytest.mark.asyncio
async def test_execute_run_login_feeds_cookie_and_token(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """登录请求先行：Cookie 进 cookie jar、token 提取为 Authorization 头，后续用例自动携带"""
    spec = make_api_spec(alice, name="会话服务", endpoint_count=1)
    api_spec_service.set_auth_config(spec.id, alice, {
        "method": "post", "path": "/login", "body": {"username": "u", "password": "p"},
        "body_type": "form", "token_field": "access_token",
    })
    endpoint = make_api_endpoint(spec.id, method="get", path="/me")
    make_api_endpoint_case(endpoint.id, name="正常请求", request_json="{}", expected_status=200)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"access_token": "tok123"}, headers={"Set-Cookie": "session=abc123; Path=/"})
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"user": "u"})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    assert seen["cookie"] == "session=abc123"
    assert seen["authorization"] == "Bearer tok123"
    view = test_run_service.get_run_view(run.id, alice)
    assert view["status"] == "completed"
    assert view["passed"] == 1
    # 登录请求不计入用例结果
    assert view["total"] == 1 and len(view["results"]) == 1
    events = [e["event"] for e in handle.events]
    assert events == ["run_started", "auth_done", "case_done", "completed"]
    assert handle.events[0]["auth"] == {"method": "POST", "path": "/login"}
    assert handle.events[1]["ok"] is True


@pytest.mark.asyncio
async def test_execute_run_auth_failure_fails_run(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """登录失败 → 本轮 failed、无用例请求，失败原因落库并下发"""
    spec = make_api_spec(alice, name="会话服务", endpoint_count=1)
    api_spec_service.set_auth_config(spec.id, alice, {
        "method": "post", "path": "/login", "body": {"username": "u", "password": "bad"},
        "body_type": "json", "token_field": None,
    })
    endpoint = make_api_endpoint(spec.id, method="get", path="/me")
    make_api_endpoint_case(endpoint.id, name="正常请求", request_json="{}", expected_status=200)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad credentials"})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    view = test_run_service.get_run_view(run.id, alice)
    assert view["status"] == "failed"
    assert view["results"] == []
    assert "登录态获取失败" in view["error"]
    events = [e["event"] for e in handle.events]
    assert events == ["run_started", "auth_done", "failed"]
    assert handle.events[1]["ok"] is False
    assert handle.events[2]["error"].startswith("登录态获取失败")


# ---------- 登录请求体媒体类型以接口声明优先（D-025 细化） ----------


@pytest.mark.asyncio
async def test_execute_run_auth_media_type_follows_endpoint_declaration(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """配置为 JSON 而接口声明表单时，登录请求按表单发送（表单端点收 JSON 必 422）"""
    spec = make_api_spec(alice, name="会话服务", endpoint_count=2)
    make_api_endpoint(
        spec.id, method="post", path="/login",
        request_body_json=json.dumps({"type": "object", "properties": {"username": {"type": "string"}}}),
        request_body_media_type="application/x-www-form-urlencoded",
    )
    api_spec_service.set_auth_config(spec.id, alice, {
        "method": "post", "path": "/login", "body": {"username": "u", "password": "p"},
        "body_type": "json", "token_field": None,
    })
    me = make_api_endpoint(spec.id, method="get", path="/me")
    make_api_endpoint_case(me.id, name="正常请求", request_json="{}", expected_status=200)

    captures = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            captures["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, headers={"Set-Cookie": "session=abc; Path=/"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    assert captures["content_type"] == "application/x-www-form-urlencoded"
    view = test_run_service.get_run_view(run.id, alice)
    assert view["status"] == "completed" and view["passed"] == 1
    assert handle.events[1]["ok"] is True


@pytest.mark.asyncio
async def test_execute_run_auth_media_type_declaration_json_overrides_form_config(db_session, make_api_spec, make_api_endpoint, alice, monkeypatch):
    """反向同理：接口声明 JSON 时，配置的表单类型被声明覆盖"""
    spec = make_api_spec(alice, name="令牌服务", endpoint_count=1)
    make_api_endpoint(
        spec.id, method="post", path="/login",
        request_body_json=json.dumps({"type": "object", "properties": {"username": {"type": "string"}}}),
        request_body_media_type="application/json",
    )
    api_spec_service.set_auth_config(spec.id, alice, {
        "method": "post", "path": "/login", "body": {"username": "u"},
        "body_type": "form", "token_field": "access_token",
    })

    captures = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captures["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"access_token": "tok"})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    assert captures["content_type"] == "application/json"
    # token 照常提取
    assert handle.events[1]["ok"] is True


@pytest.mark.asyncio
async def test_execute_run_auth_422_message_hints_body_type(db_session, make_api_spec, make_api_endpoint, alice, monkeypatch):
    """登录 422 的失败信息提示检查请求体类型"""
    spec = make_api_spec(alice, name="会话服务", endpoint_count=1)
    api_spec_service.set_auth_config(spec.id, alice, {
        "method": "post", "path": "/auth", "body": {"u": 1},
        "body_type": "json", "token_field": None,
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "validation error"})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    assert handle.events[1]["message"].startswith("登录态获取失败：HTTP 422")
    assert "请求体类型" in handle.events[1]["message"]


# ---------- 执行历史可见性与执行人（D-023） ----------


def _make_bob_client(app) -> TestClient:
    bob = TestClient(app)
    bob.post("/register", data={"username": "bob", "password": "secret123"}, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "secret123"}, follow_redirects=False)
    return bob


def test_run_history_visible_to_spec_readers_with_executor(
    app, logged_in_client, db_session, make_user, make_api_spec, make_api_endpoint, make_api_endpoint_case
):
    """D-023：执行历史面向规格可读者开放并展示执行人；无关用户仍 404"""
    alice_id = _alice_id(db_session)
    spec = make_api_spec(alice_id, name="目标服务")
    endpoint = make_api_endpoint(spec.id, method="get", path="/ping")
    make_api_endpoint_case(endpoint.id, name="正常请求", request_json="{}", expected_status=200)
    run = test_run_service.try_claim_run(spec.id, alice_id, "http://target.example")

    bob = _make_bob_client(app)

    # 私有规格：无关用户历史与详情均 404
    assert bob.get(f"/api/api-specs/{spec.id}/runs").status_code == 404
    assert bob.get(f"/api/test-runs/{run.id}").status_code == 404

    # 共享后：可读者可见全部运行（含执行人）与逐条详情
    spec.visibility = "shared"
    db_session.commit()

    history = bob.get(f"/api/api-specs/{spec.id}/runs")
    assert history.status_code == 200
    runs = history.json()
    assert len(runs) == 1
    assert runs[0]["created_by"] == alice_id
    assert runs[0]["created_by_username"] == "alice"

    detail = bob.get(f"/api/test-runs/{run.id}")
    assert detail.status_code == 200
    assert detail.json()["created_by_username"] == "alice"

    # 规格消失后（被删除）：可读者失去访问权，执行人本人仍可回看自己的执行
    db_session.execute(text("DELETE FROM api_specs WHERE id = :id"), {"id": spec.id})
    db_session.commit()
    assert bob.get(f"/api/test-runs/{run.id}").status_code == 404
    assert logged_in_client.get(f"/api/test-runs/{run.id}").status_code == 200
