"""接口用例：规则引擎（纯函数）、AI 建议、整表替换（服务层与端点）"""

import json

import pytest

from models.api_test_models import ApiEndpointCase
from models.user import User
from schemas.api_test_schemas import ApiCaseProposal, ApiCaseProposalSet
from services import api_case_engine, api_case_service


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


BODY_SCHEMA = {
    "type": "object",
    "required": ["username", "age"],
    "properties": {
        "username": {"type": "string", "minLength": 3, "maxLength": 20},
        "age": {"type": "integer", "minimum": 1, "maximum": 120},
        "role": {"type": "string", "enum": ["admin", "user"]},
    },
}

ENDPOINT_POST = {
    "method": "post",
    "path": "/users",
    "parameters": [],
    "request_body": BODY_SCHEMA,
    "responses": {"201": "created", "400": "bad request"},
}

ENDPOINT_GET_QUERY = {
    "method": "get",
    "path": "/users",
    "parameters": [
        {"name": "page", "in": "query", "required": True, "schema": {"type": "integer", "minimum": 1}},
        {"name": "size", "in": "query", "schema": {"type": "integer", "maximum": 100}},
    ],
    "request_body": "",
    "responses": {},
}


def _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, user_id):
    spec = make_api_spec(user_id, name="用户服务", endpoint_count=1)
    endpoint = make_api_endpoint(
        spec.id,
        method="post",
        path="/users",
        request_body_json=json.dumps(BODY_SCHEMA),
        responses_json=json.dumps({"201": "created", "400": "bad request"}),
    )
    return spec, endpoint


# ---------- 规则引擎（纯函数） ----------


def test_engine_normal_case_uses_declared_success_status():
    proposals = api_case_engine.generate_case_proposals(ENDPOINT_POST)

    normal = proposals[0]
    assert normal["name"] == "正常请求"
    assert normal["expected_status"] == 201  # 声明的首个 2xx
    assert normal["source_type"] == "rule_engine"
    # 只填 required 属性（最小合法）；enum 首值优先
    assert normal["request"]["body"] == {"username": "test-username", "age": 1}


def test_engine_body_anomalies_per_dimension():
    proposals = api_case_engine.generate_case_proposals(ENDPOINT_POST)
    names = [p["name"] for p in proposals]

    assert "缺失必填字段 - 400" in names
    assert "类型错误 username - 400" in names
    assert "类型错误 age - 400" in names
    assert "超出上限 age - 400" in names
    assert all(p["expected_status"] == 400 for p in proposals if p["name"] != "正常请求")

    type_case = next(p for p in proposals if p["name"] == "类型错误 username - 400")
    assert type_case["request"]["body"]["username"] == 12345


def test_engine_anomalies_do_not_pollute_each_other():
    """每条提案深拷贝：某条异常的字段变异不得污染其他提案与正常基线"""
    proposals = api_case_engine.generate_case_proposals(ENDPOINT_POST)

    bodies = [json.dumps(p["request"].get("body") or {}, sort_keys=True) for p in proposals]
    assert len(set(bodies)) == len(bodies)
    assert proposals[0]["request"]["body"]["age"] == 1


def test_engine_query_anomalies_and_missing_required():
    proposals = api_case_engine.generate_case_proposals(ENDPOINT_GET_QUERY)
    names = [p["name"] for p in proposals]

    assert "缺失必填查询参数 - 400" in names
    assert "类型错误 查询参数 page - 400" in names
    assert "超出上限 查询参数 size - 400" in names

    missing = next(p for p in proposals if p["name"] == "缺失必填查询参数 - 400")
    assert "page" not in missing["request"]["query"]


def test_engine_endpoint_without_body_and_schema():
    proposals = api_case_engine.generate_case_proposals({
        "method": "get", "path": "/ping", "parameters": [], "request_body": "", "responses": {},
    })
    assert len(proposals) == 1
    assert proposals[0]["expected_status"] == 200
    assert "body" not in proposals[0]["request"]


# ---------- 规则引擎：组合 schema 归一与 header 采样（D-024） ----------


def test_engine_normalizes_allof_body():
    """allOf 请求体合并 required/properties，正常请求不再采样成空对象"""
    schema = {
        "allOf": [
            {"type": "object", "required": ["username"], "properties": {"username": {"type": "string"}}},
            {"type": "object", "required": ["age"], "properties": {"age": {"type": "integer"}}},
        ]
    }
    proposals = api_case_engine.generate_case_proposals({
        "method": "post", "path": "/users", "parameters": [], "request_body": schema, "responses": {"200": "ok"},
    })
    normal = proposals[0]
    assert normal["request"]["body"] == {"username": "test-username", "age": 1}
    # 合并后的 required 全量缺失用例
    missing = next(p for p in proposals if p["name"] == "缺失必填字段 - 400")
    assert missing["request"]["body"] == {}


def test_engine_normalizes_oneof_body_takes_first_branch():
    schema = {
        "oneOf": [
            {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
            {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
        ]
    }
    proposals = api_case_engine.generate_case_proposals({
        "method": "post", "path": "/pets", "parameters": [], "request_body": schema, "responses": {},
    })
    assert proposals[0]["request"]["body"] == {"name": "test-name"}


def test_engine_samples_required_header_params():
    endpoint = {
        "method": "get", "path": "/reports",
        "parameters": [
            {"name": "X-Trace-Id", "in": "header", "required": True, "schema": {"type": "string"}},
            {"name": "page", "in": "query", "schema": {"type": "integer"}},
        ],
        "request_body": "",
        "responses": {},
    }
    proposals = api_case_engine.generate_case_proposals(endpoint)
    assert proposals[0]["request"]["headers"] == {"X-Trace-Id": "test-X-Trace-Id"}
    # 异常提案的 headers 深拷贝自基线，不被 query/body 变异污染
    anomaly = next(p for p in proposals if p["name"].startswith("类型错误"))
    assert anomaly["request"]["headers"] == {"X-Trace-Id": "test-X-Trace-Id"}


def test_engine_samples_binary_file_fields():
    schema = {
        "type": "object",
        "required": ["file"],
        "properties": {"file": {"type": "string", "format": "binary"}, "note": {"type": "string"}},
    }
    proposals = api_case_engine.generate_case_proposals({
        "method": "post", "path": "/upload", "parameters": [], "request_body": schema, "responses": {},
    })
    assert proposals[0]["request"]["body"]["file"] == "test-file.bin"


# ---------- 服务层：生成 / 整表替换 / AI 建议 ----------


def test_generate_cases_replaces_rule_engine_keeps_manual(db_session, make_api_spec, make_api_endpoint, alice):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    db_session.add(ApiEndpointCase(
        endpoint_id=endpoint.id, name="手工用例",
        request_json="{}", expected_status=200, source_type="manual",
    ))
    db_session.commit()

    cases = api_case_service.generate_cases(spec.id, endpoint.id, alice)

    names = [c["name"] for c in cases]
    assert "手工用例" in names
    assert "正常请求" in names and "缺失必填字段 - 400" in names
    assert len([c for c in cases if c["source_type"] == "rule_engine"]) >= 4


def test_generate_cases_name_conflict_with_manual(db_session, make_api_spec, make_api_endpoint, alice):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    db_session.add(ApiEndpointCase(
        endpoint_id=endpoint.id, name="正常请求",
        request_json="{}", expected_status=200, source_type="manual",
    ))
    db_session.commit()

    with pytest.raises(ValueError):
        api_case_service.generate_cases(spec.id, endpoint.id, alice)


def test_save_cases_replaces_all(db_session, make_api_spec, make_api_endpoint, alice):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    api_case_service.generate_cases(spec.id, endpoint.id, alice)

    trimmed = [{
        "name": "保留一",
        "request": {"body": {"username": "abc", "age": 1}},
        "expected_status": 201,
        "source_type": "manual",
        "enabled": True,
    }]
    result = api_case_service.save_cases(spec.id, endpoint.id, alice, trimmed)

    assert [c["name"] for c in result] == ["保留一"]
    assert len(api_case_service.list_cases(spec.id, endpoint.id, alice)) == 1


def test_save_cases_duplicate_names_rejected(db_session, make_api_spec, make_api_endpoint, alice):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    duplicated = [
        {"name": "同名", "request": {}, "expected_status": 200, "source_type": "manual", "enabled": True},
        {"name": "同名", "request": {}, "expected_status": 200, "source_type": "manual", "enabled": True},
    ]
    with pytest.raises(ValueError):
        api_case_service.save_cases(spec.id, endpoint.id, alice, duplicated)


def test_list_cases_shared_reader_allowed_but_generate_owner_only(
    db_session, make_api_spec, make_api_endpoint, make_user, alice, bob
):
    spec = make_api_spec(bob, name="共享规格", visibility="shared")
    endpoint = make_api_endpoint(spec.id, method="post", path="/users",
                                 request_body_json=json.dumps(BODY_SCHEMA))

    cases = api_case_service.list_cases(spec.id, endpoint.id, alice)
    assert cases == []

    with pytest.raises(api_case_service.NotFoundError):
        api_case_service.generate_cases(spec.id, endpoint.id, alice)  # 写操作 owner-only（404 语义）


# ---------- AI 业务建议 ----------


@pytest.mark.asyncio
async def test_ai_suggest_returns_proposals(db_session, make_api_spec, make_api_endpoint, alice, stub_workflow_llm):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    stub_workflow_llm.case_proposals = ApiCaseProposalSet(proposals=[
        ApiCaseProposal(name="未登录调用 - 401", request={"headers": {}}, expected_status=401),
        ApiCaseProposal(name="重复提交 - 409", request={"body": {"username": "test-username", "age": 1}}, expected_status=409),
    ])

    payload = await api_case_service.ai_suggest_cases(spec.id, endpoint.id, alice, "补充权限与并发场景")

    assert payload["endpoint_id"] == endpoint.id
    assert payload["truncated"] is False
    assert [p["name"] for p in payload["proposals"]] == ["未登录调用 - 401", "重复提交 - 409"]


@pytest.mark.asyncio
async def test_ai_suggest_retries_once_after_llm_failure(db_session, make_api_spec, make_api_endpoint, alice, stub_workflow_llm):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    stub_workflow_llm.fail_times = 1  # 第一次调用失败、第二次成功
    stub_workflow_llm.case_proposals = ApiCaseProposalSet(proposals=[
        ApiCaseProposal(name="资源不存在 - 404", request={}, expected_status=404),
    ])

    payload = await api_case_service.ai_suggest_cases(spec.id, endpoint.id, alice, "补充资源场景")

    assert stub_workflow_llm.calls == 2
    assert payload["proposals"][0]["name"] == "资源不存在 - 404"


@pytest.mark.asyncio
async def test_ai_suggest_duplicate_names_fail_after_retry(db_session, make_api_spec, make_api_endpoint, alice, stub_workflow_llm):
    """提案重名属可重试校验失败：两次尝试都重名 → AISuggestError 且桩被调 2 次"""
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    stub_workflow_llm.case_proposals = ApiCaseProposalSet(proposals=[
        ApiCaseProposal(name="同名", request={}, expected_status=400),
        ApiCaseProposal(name="同名", request={}, expected_status=400),
    ])

    with pytest.raises(api_case_service.AISuggestError):
        await api_case_service.ai_suggest_cases(spec.id, endpoint.id, alice, "补充场景")
    assert stub_workflow_llm.calls == 2


@pytest.mark.asyncio
async def test_ai_suggest_fails_after_two_attempts(db_session, make_api_spec, make_api_endpoint, alice, stub_workflow_llm):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    stub_workflow_llm.fail_times = 5  # 两次尝试都抛异常

    with pytest.raises(api_case_service.AISuggestError):
        await api_case_service.ai_suggest_cases(spec.id, endpoint.id, alice, "补充场景")
    assert stub_workflow_llm.calls == 2


@pytest.mark.asyncio
async def test_ai_suggest_owner_only(db_session, make_api_spec, make_api_endpoint, alice, bob, stub_workflow_llm):
    spec = make_api_spec(alice, name="我的规格")
    endpoint = make_api_endpoint(spec.id, method="post", path="/users")

    with pytest.raises(api_case_service.NotFoundError):
        await api_case_service.ai_suggest_cases(spec.id, endpoint.id, bob, "补充场景")


# ---------- 端点 ----------


def test_case_endpoints_require_login(client, db_session, make_user, make_api_spec, make_api_endpoint):
    make_user("alice", "secret123")
    spec = make_api_spec(db_session.query(User).filter(User.username == "alice").first().id)
    endpoint = make_api_endpoint(spec.id)

    assert client.get(f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases").status_code == 401
    assert client.post(f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases/generate").status_code == 401
    assert client.put(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases", json={"cases": []}
    ).status_code == 401
    assert client.post(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases/ai-suggest", json={"instruction": "x"}
    ).status_code == 401


def test_case_endpoints_owner_only_and_happy(
    logged_in_client, db_session, make_user, make_api_spec, make_api_endpoint, stub_workflow_llm
):
    make_user("bob", "secret123")
    bob_spec = make_api_spec(_bob_id(db_session))
    bob_endpoint = make_api_endpoint(bob_spec.id)
    # bob 的 private 规格对 alice 不可见 → 404
    assert logged_in_client.get(
        f"/api/api-specs/{bob_spec.id}/endpoints/{bob_endpoint.id}/cases"
    ).status_code == 404
    assert logged_in_client.post(
        f"/api/api-specs/{bob_spec.id}/endpoints/{bob_endpoint.id}/cases/generate"
    ).status_code == 404

    spec = make_api_spec(_alice_id(db_session), name="用户服务")
    endpoint = make_api_endpoint(
        spec.id, method="post", path="/users",
        request_body_json=json.dumps(BODY_SCHEMA),
        responses_json=json.dumps({"201": "created"}),
    )

    # 规则引擎生成
    response = logged_in_client.post(f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases/generate")
    assert response.status_code == 200
    assert "正常请求" in [c["name"] for c in response.json()]

    # 整表替换
    response = logged_in_client.put(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases",
        json={"cases": [{
            "name": "正常请求", "request": {"body": {"username": "abc", "age": 1}},
            "expected_status": 201, "source_type": "manual", "enabled": True,
        }]},
    )
    assert response.status_code == 200
    assert [c["source_type"] for c in response.json()] == ["manual"]

    # AI 建议（桩）
    stub_workflow_llm.case_proposals = ApiCaseProposalSet(proposals=[
        ApiCaseProposal(name="未登录 - 401", request={}, expected_status=401),
    ])
    response = logged_in_client.post(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases/ai-suggest",
        json={"instruction": "补充权限场景"},
    )
    assert response.status_code == 200
    assert response.json()["proposals"][0]["name"] == "未登录 - 401"

    # AI 建议 LLM 失败 → 502 通用文案
    stub_workflow_llm.fail_times = 5
    response = logged_in_client.post(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases/ai-suggest",
        json={"instruction": "补充权限场景"},
    )
    assert response.status_code == 502
    assert response.json() == {"error": "AI 建议生成失败，请稍后重试"}


def test_ai_suggest_instruction_length_validated(logged_in_client, db_session, make_api_spec, make_api_endpoint):
    spec = make_api_spec(_alice_id(db_session))
    endpoint = make_api_endpoint(spec.id)

    response = logged_in_client.post(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases/ai-suggest",
        json={"instruction": ""},
    )
    assert response.status_code == 422
    response = logged_in_client.post(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases/ai-suggest",
        json={"instruction": "字" * 2001},
    )
    assert response.status_code == 422
