"""测试执行：占用语义、httpx 执行器（MockTransport 桩）、结果落库、SSE 事件流、权限"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.endpoints.run_hub import RunHub
from conftest import parse_sse_events
from models.api_test_models import TestRun, TestRunResult
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
async def test_execute_run_body_assertions(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """响应体断言（D-027）：状态码匹配但断言不符 → failed；结果与 case_done 携带断言明细与接口标签"""
    spec = make_api_spec(alice, name="目标服务", endpoint_count=2)
    ok_ep = make_api_endpoint(spec.id, method="get", path="/ok")
    make_api_endpoint_case(
        ok_ep.id, name="断言通过",
        request_json=json.dumps({"query": {}}),
        assertions_json=json.dumps([
            {"target": "code", "op": "eq", "expected": 0},
            {"target": "data.id", "op": "exists", "expected": None},
        ]),
    )
    bad_ep = make_api_endpoint(spec.id, method="get", path="/bad")
    make_api_endpoint_case(
        bad_ep.id, name="断言失败",
        request_json=json.dumps({"query": {}}),
        assertions_json=json.dumps([
            {"target": "code", "op": "eq", "expected": 0},
            {"target": "data.id", "op": "type", "expected": "integer"},
        ]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            return httpx.Response(200, json={"code": 0, "data": {"id": 7}})
        return httpx.Response(200, json={"code": -1, "data": {"id": "not-int"}})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")

    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    view = test_run_service.get_run_view(run.id, alice)
    assert view["passed"] == 1 and view["failed"] == 1 and view["errored"] == 0

    ok_result = next(r for r in view["results"] if r["verdict"] == "passed")
    assert all(a["passed"] for a in ok_result["assertions"])
    assert ok_result["failure_reason"] is None
    assert ok_result["endpoint"] == "GET /ok"

    bad_result = next(r for r in view["results"] if r["verdict"] == "failed")
    assert bad_result["actual_status"] == 200  # 状态码本身匹配，败在断言
    assert bad_result["failure_reason"] == "断言失败：code 期望 0，实际 -1；data.id 实际类型 string"
    assert bad_result["assertions"][0]["passed"] is False
    assert bad_result["endpoint"] == "GET /bad"

    case_done = {e["case_name"]: e for e in handle.events if e["event"] == "case_done"}
    assert case_done["断言失败"]["endpoint"] == "GET /bad"
    assert case_done["断言失败"]["assertions"][0]["passed"] is False
    assert case_done["断言通过"]["assertions"][0]["passed"] is True


@pytest.mark.asyncio
async def test_execute_run_body_assertions_non_json(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """响应体非合法 JSON：带断言的用例一律失败并给出明确文案"""
    spec = make_api_spec(alice, name="目标服务", endpoint_count=1)
    endpoint = make_api_endpoint(spec.id, method="get", path="/text")
    make_api_endpoint_case(
        endpoint.id, name="文本响应",
        request_json=json.dumps({"query": {}}),
        assertions_json=json.dumps([{"target": "code", "op": "eq", "expected": 0}]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>")

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")

    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    view = test_run_service.get_run_view(run.id, alice)
    result = view["results"][0]
    assert result["verdict"] == "failed"
    assert result["failure_reason"] == "断言失败：code 响应体不是合法 JSON"
    assert result["assertions"][0]["message"] == "响应体不是合法 JSON"
    assert [e["event"] for e in handle.events] == ["run_started", "case_done", "completed"]


@pytest.mark.asyncio
async def test_execute_run_endpoint_ids_subset_and_labels(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """endpoint_ids 子集执行：只跑所选接口的用例；事件与结果携带接口标签"""
    spec = make_api_spec(alice, name="目标服务", endpoint_count=2)
    alpha = make_api_endpoint(spec.id, method="get", path="/alpha")
    make_api_endpoint_case(alpha.id, name="A 用例", request_json=json.dumps({"query": {}}))
    beta = make_api_endpoint(spec.id, method="get", path="/beta")
    make_api_endpoint_case(beta.id, name="B 用例", request_json=json.dumps({"query": {}}))

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example", endpoint_ids=[beta.id])

    hub = RunHub()
    handle = hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", [beta.id], hub)

    view = test_run_service.get_run_view(run.id, alice)
    assert view["total"] == 1
    assert [r["case_name"] for r in view["results"]] == ["B 用例"]
    assert view["results"][0]["endpoint"] == "GET /beta"
    assert calls == ["http://target.example/beta"]
    assert [e["endpoint"] for e in handle.events if e["event"] == "case_done"] == ["GET /beta"]


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


# ---------- 请求快照凭据脱敏（执行详情对规格可读者开放，凭据不得跨用户可见） ----------


@pytest.mark.asyncio
async def test_execute_run_redacts_auth_headers_in_snapshot(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """登录态派生的 Authorization 与用例自带的 x-api-key 均以 "***" 落库与下发"""
    spec = make_api_spec(alice, name="会话服务", endpoint_count=1)
    api_spec_service.set_auth_config(spec.id, alice, {
        "method": "post", "path": "/login", "body": {"u": 1},
        "body_type": "json", "token_field": "access_token",
    })
    endpoint = make_api_endpoint(spec.id, method="get", path="/me")
    make_api_endpoint_case(
        endpoint.id, name="正常请求",
        request_json=json.dumps({"headers": {"x-api-key": "secret-key", "accept": "application/json"}}),
        expected_status=200,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"access_token": "tok123"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    view = test_run_service.get_run_view(run.id, alice)
    headers = view["results"][0]["request"]["headers"]
    # 脱敏按头名不区分大小写匹配，保留原始键大小写
    assert headers["Authorization"] == "***"
    assert headers["x-api-key"] == "***"
    assert headers["accept"] == "application/json"
    assert "tok123" not in json.dumps(view["results"][0])


def test_run_view_redacts_legacy_stored_snapshot(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice):
    """写侧脱敏上线前落库的存量结果：读取时兜底脱敏（读侧防线）"""
    spec, endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    db_session.add(TestRunResult(
        run_id=run.id, endpoint_id=endpoint.id, case_name="存量用例",
        request_json=json.dumps({"headers": {"Authorization": "Bearer legacy-token", "accept": "*/*"}}),
        response_json="{}", verdict="passed", expected_status=200, actual_status=200,
    ))
    db_session.commit()

    view = test_run_service.get_run_view(run.id, alice)
    headers = view["results"][0]["request"]["headers"]
    assert headers["Authorization"] == "***"
    assert headers["accept"] == "*/*"


def test_redact_stored_request_headers_rewrites_history(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice):
    """启动清洗：存量行明文头被改写为 "***"，且二次调用幂等不再改写"""
    spec, endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    raw = json.dumps({"headers": {"authorization": "Bearer legacy-token"}})
    db_session.add(TestRunResult(
        run_id=run.id, endpoint_id=endpoint.id, case_name="存量用例",
        request_json=raw, response_json="{}", verdict="passed", expected_status=200, actual_status=200,
    ))
    db_session.commit()

    assert test_run_service.redact_stored_request_headers() == 1
    db_session.expire_all()
    stored = json.loads(db_session.query(TestRunResult).filter(TestRunResult.run_id == run.id).first().request_json)
    assert stored["headers"]["authorization"] == "***"

    assert test_run_service.redact_stored_request_headers() == 0


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


@pytest.mark.asyncio
async def test_execute_run_encodes_path_params(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice, monkeypatch):
    """path 参数先编码再代入模板：含 / 或空格的取值不编码会把单个参数拆成多段路径"""
    spec = make_api_spec(alice, name="目标服务", endpoint_count=1)
    endpoint = make_api_endpoint(spec.id, method="get", path="/files/{name}")
    make_api_endpoint_case(
        endpoint.id, name="正常请求",
        request_json=json.dumps({"path": {"name": "报告/2024 v1.pdf"}}),
        expected_status=200,
    )

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw_path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(test_run_service, "_create_run_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")
    hub = RunHub()
    hub.register(run.id, lambda: None)
    await test_run_service.execute_run(run.id, "http://target.example", None, hub)

    # 斜杠以 %2F 保留在单个路径段内，而非拆出新的路径层级
    assert seen["raw_path"] == "/files/%E6%8A%A5%E5%91%8A%2F2024%20v1.pdf"


# ---------- SQLite 外键约束与 MySQL 生产语义一致（R7） ----------


def test_sqlite_foreign_keys_enforced(db_session):
    """测试库必须开启外键约束：否则 ondelete CASCADE/SET NULL 静默失效，与 MySQL 分叉"""
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_deleting_spec_nulls_test_run_reference(
    db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice
):
    """删除接口文档 → 执行记录的 spec_id 置空（生产 MySQL 的 SET NULL 语义）"""
    spec, _endpoint = _spec_with_case(db_session, make_api_spec, make_api_endpoint, make_api_endpoint_case, alice)
    run = test_run_service.try_claim_run(spec.id, alice, "http://target.example")

    db_session.execute(text("DELETE FROM api_specs WHERE id = :id"), {"id": spec.id})
    db_session.commit()
    db_session.expire_all()

    refreshed = db_session.query(TestRun).filter(TestRun.id == run.id).first()
    assert refreshed is not None
    assert refreshed.spec_id is None
