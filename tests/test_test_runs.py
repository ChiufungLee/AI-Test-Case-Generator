"""测试执行：占用语义、httpx 执行器（MockTransport 桩）、结果落库、SSE 事件流、权限"""

import json

import httpx
import pytest

from api.endpoints.run_hub import RunHub
from conftest import parse_sse_events
from models.api_test_models import TestRun
from models.user import User
from services import test_run_service
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

    monkeypatch.setattr(test_run_service, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
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

    monkeypatch.setattr(test_run_service, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
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

    monkeypatch.setattr(test_run_service, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
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
        test_run_service, "_client",
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
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
