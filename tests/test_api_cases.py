"""接口用例：规则引擎（纯函数）、AI 建议、整表替换（服务层与端点）"""

import json
import re

import pytest

from models.api_test_models import ApiEndpointCase
from models.user import User
from schemas.api_test_schemas import ApiCaseAssertion, ApiCaseProposal, ApiCaseProposalSet
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

RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["code", "data"],
    "properties": {
        "code": {"type": "integer"},
        "data": {"type": "object"},
        "message": {"type": "string"},
    },
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


def test_engine_normal_case_derives_body_assertions():
    """正常用例按响应 schema 顶层 required 派生 exists/type 断言；异常用例断言为空（D-027）"""
    endpoint = {**ENDPOINT_POST, "response_schemas": {"201": RESPONSE_SCHEMA}}
    proposals = api_case_engine.generate_case_proposals(endpoint)

    normal = proposals[0]
    assert normal["assertions"] == [
        {"target": "code", "op": "exists", "expected": None},
        {"target": "code", "op": "type", "expected": "integer"},
        {"target": "data", "op": "exists", "expected": None},
        {"target": "data", "op": "type", "expected": "object"},
    ]
    assert all(p["assertions"] == [] for p in proposals[1:])


def test_engine_no_assertions_without_response_schema():
    proposals = api_case_engine.generate_case_proposals(ENDPOINT_POST)
    assert all(p["assertions"] == [] for p in proposals)


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


def test_save_cases_roundtrips_assertions(db_session, make_api_spec, make_api_endpoint, alice):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    cases = [{
        "name": "响应字段断言",
        "request": {"body": {"username": "abc", "age": 1}},
        "expected_status": 201,
        "assertions": [
            {"target": "code", "op": "eq", "expected": 0},
            {"target": "data.id", "op": "exists", "expected": None},
        ],
        "source_type": "manual",
        "enabled": True,
    }]

    result = api_case_service.save_cases(spec.id, endpoint.id, alice, cases)

    assert result[0]["assertions"] == cases[0]["assertions"]
    row = db_session.query(ApiEndpointCase).filter(ApiEndpointCase.name == "响应字段断言").first()
    assert json.loads(row.assertions_json)[0]["target"] == "code"


def test_generate_cases_persists_derived_assertions(db_session, make_api_spec, make_api_endpoint, alice):
    spec = make_api_spec(alice, name="用户服务", endpoint_count=1)
    endpoint = make_api_endpoint(
        spec.id, method="post", path="/users",
        request_body_json=json.dumps(BODY_SCHEMA),
        responses_json=json.dumps({"201": "created"}),
        response_schemas_json=json.dumps({"201": RESPONSE_SCHEMA}),
    )

    cases = api_case_service.generate_cases(spec.id, endpoint.id, alice)

    normal = next(c for c in cases if c["name"] == "正常请求")
    assert [a["op"] for a in normal["assertions"]] == ["exists", "type", "exists", "type"]


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
async def test_ai_suggest_proposals_carry_assertions(db_session, make_api_spec, make_api_endpoint, alice, stub_workflow_llm):
    spec, endpoint = _endpoint_with_body(db_session, make_api_spec, make_api_endpoint, alice)
    stub_workflow_llm.case_proposals = ApiCaseProposalSet(proposals=[
        ApiCaseProposal(
            name="重复提交 - 409", request={}, expected_status=409,
            assertions=[ApiCaseAssertion(target="code", op="eq", expected=1001)],
        ),
    ])

    payload = await api_case_service.ai_suggest_cases(spec.id, endpoint.id, alice, "补充并发场景")

    assert payload["proposals"][0]["assertions"] == [{"target": "code", "op": "eq", "expected": 1001}]


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


def test_save_cases_invalid_assertion_rejected(logged_in_client, db_session, make_api_spec, make_api_endpoint):
    """非法断言（未知 op）经 Pydantic 校验拒绝 → 422"""
    spec = make_api_spec(_alice_id(db_session))
    endpoint = make_api_endpoint(spec.id)

    response = logged_in_client.put(
        f"/api/api-specs/{spec.id}/endpoints/{endpoint.id}/cases",
        json={"cases": [{
            "name": "坏断言", "request": {}, "expected_status": 200,
            "assertions": [{"target": "code", "op": "between", "expected": 1}],
            "source_type": "manual", "enabled": True,
        }]},
    )
    assert response.status_code == 422


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


# ---------- 合成值上限：文档里的数值完全由作者控制，不得放大成超大分配 ----------


def _body_endpoint(properties: dict, required: list[str]) -> dict:
    return {
        "method": "post",
        "path": "/x",
        "parameters": [],
        "request_body": {"type": "object", "required": required, "properties": properties},
    }


def test_absurd_max_length_skips_range_dimension():
    """maxLength 过大时不再生成"超出上限"维度（此前会分配 maxLength+1 个字符）"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint({"name": {"type": "string", "maxLength": 500_000_000}}, ["name"])
    )

    assert all("超出上限" not in proposal["name"] for proposal in proposals)
    for proposal in proposals:
        assert len(json.dumps(proposal["request"], ensure_ascii=False)) < 10_000


def test_absurd_min_length_and_min_items_are_capped():
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint(
            {
                "note": {"type": "string", "minLength": 5_000_000},
                "ids": {"type": "array", "minItems": 200_000, "items": {"type": "integer"}},
            },
            ["note", "ids"],
        )
    )

    body = proposals[0]["request"]["body"]
    assert len(body["note"]) == api_case_engine._MAX_SYNTHETIC_LENGTH
    assert len(body["ids"]) == api_case_engine._MAX_SYNTHETIC_ITEMS
    assert len(json.dumps(body, ensure_ascii=False)) < 10_000


def test_v3_path_param_samples_from_nested_schema():
    """3.x 的约束在 param["schema"]：此前把整个参数对象当 schema，integer+enum 退化成 test-id"""
    proposals = api_case_engine.generate_case_proposals({
        "method": "get",
        "path": "/users/{id}",
        "parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "integer", "enum": [7, 8]}},
        ],
        "request_body": None,
    })

    assert proposals[0]["request"]["path"]["id"] == 7


def test_swagger2_param_samples_from_top_level_type():
    """2.0 的 path/query/header 参数把 type/enum/format 直接写在参数上（无 schema 键）"""
    proposals = api_case_engine.generate_case_proposals({
        "method": "get",
        "path": "/items",
        "parameters": [
            {"name": "page", "in": "query", "type": "integer", "enum": [1, 2]},
            {"name": "X-Trace", "in": "header", "type": "string", "format": "uuid"},
        ],
        "request_body": None,
    })

    request = proposals[0]["request"]
    assert request["query"]["page"] == 1
    assert request["headers"]["X-Trace"] == "3fa85f64-5717-4562-b3fc-2c963f66afa6"


# ---------- 字段路径 / 隐式类型 / 开区间 / 区间状态码 ----------


def test_property_name_with_dot_targets_the_real_key():
    """属性名含 "." 时不得按点号切路径：此前会抛 TypeError（改错字段），生成接口 500"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint(
            {"a": {"type": "string", "maxLength": 5}, "a.b": {"type": "string", "maxLength": 5}},
            ["a", "a.b"],
        )
    )

    type_errors = [p for p in proposals if p["name"].startswith("类型错误 a.b")]
    assert type_errors
    body = type_errors[0]["request"]["body"]
    assert body["a.b"] == 12345  # 真实键被替换为类型错误值
    assert body["a"] == "test-"  # 同名前缀字段不受影响


def test_implicit_object_schema_is_sampled_as_object():
    """省略 type 的隐式对象（只有 properties/required）必须按对象采样，而不是 "test-o" 字符串"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint({"o": {"properties": {"x": {"type": "string"}}, "required": ["x"]}}, ["o"])
    )

    assert proposals[0]["request"]["body"]["o"] == {"x": "test-x"}


def test_pattern_violation_value_is_actually_invalid():
    """违反 pattern 的取值必须真的不匹配：固定值会匹配 "^[a-z_]+$"，生成预期 400 的假失败"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint({"code": {"type": "string", "pattern": "^[a-z_]+$"}}, ["code"])
    )
    pattern_cases = [p for p in proposals if p["name"].startswith("违反格式")]
    assert pattern_cases
    assert re.fullmatch(r"[a-z_]+", pattern_cases[0]["request"]["body"]["code"]) is None

    # 任何取值都满足的 pattern：构造不出违反值 → 不生成该维度（否则必然稳定误报）
    all_match = api_case_engine.generate_case_proposals(
        _body_endpoint({"code": {"type": "string", "pattern": ".*"}}, ["code"])
    )
    assert not [p for p in all_match if p["name"].startswith("违反格式")]


def test_exclusive_bounds_are_respected():
    """开区间：正常样例必须满足 exclusiveMinimum；越界维度要覆盖 exclusiveMaximum"""
    normal = api_case_engine.generate_case_proposals(
        _body_endpoint({"n": {"type": "integer", "minimum": 0, "exclusiveMinimum": 0}}, ["n"])
    )[0]
    assert normal["request"]["body"]["n"] == 1  # 采样 0 会违反 >0

    only_max = api_case_engine.generate_case_proposals(
        _body_endpoint({"n": {"type": "integer", "exclusiveMaximum": 10}}, ["n"])
    )
    over_limit = [p for p in only_max if p["name"].startswith("超出上限")]
    assert over_limit
    assert over_limit[0]["request"]["body"]["n"] == 10  # 开区间：等于上界即越界

    # draft-04 的布尔 exclusiveMinimum 不是数值约束：没有 minimum 时不得凭空造越界值
    boolean_form = api_case_engine.generate_case_proposals(
        _body_endpoint({"n": {"type": "integer", "exclusiveMinimum": True}}, ["n"])
    )
    assert not [p for p in boolean_form if p["name"].startswith("低于下限")]


def test_required_field_without_properties_gets_placeholder():
    """required 声明但 properties 未定义的字段不能静默丢弃：正常用例缺必填会被 422"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint({"a": {"type": "string"}}, ["a", "b"])
    )

    assert proposals[0]["request"]["body"] == {"a": "test-a", "b": "test-b"}
    missing = [p for p in proposals if p["name"].startswith("缺失必填")]
    assert missing and missing[0]["request"]["body"] == {}


def test_range_status_code_derives_assertions_from_range_key():
    """文档只声明 2XX 时，响应断言要按区间码取 schema 派生（此前用 "200" 取键必然取空）"""
    proposals = api_case_engine.generate_case_proposals({
        "method": "post",
        "path": "/x",
        "parameters": [],
        "request_body": {
            "type": "object",
            "required": ["a"],
            "properties": {"a": {"type": "string"}},
        },
        "responses": {"2XX": "ok"},
        "response_schemas": {
            "2XX": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
        },
    })

    normal = proposals[0]
    assert normal["expected_status"] == 200
    assert {"target": "id", "op": "exists", "expected": None} in normal["assertions"]


# ---------- 数组元素异常 / 逐维限额跨递归共享 / 下钻深度上限 ----------


def test_array_item_anomalies_are_generated():
    """items 的约束此前完全没被覆盖：数组元素的类型错误/越界必须生成"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint({"tags": {"type": "array", "items": {"type": "string", "maxLength": 5}}}, ["tags"])
    )
    by_name = {p["name"]: p for p in proposals}

    assert by_name["类型错误 tags[0] - 400"]["request"]["body"]["tags"] == [12345]
    assert by_name["超出上限 tags[0] - 400"]["request"]["body"]["tags"] == ["xxxxxx"]


def test_array_of_objects_descends_into_element_properties():
    """数组元素是对象时按元素属性继续下钻（展示名为 field[0].prop）"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint(
            {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["n"],
                        "properties": {"n": {"type": "integer", "maximum": 3}},
                    },
                }
            },
            ["rows"],
        )
    )
    by_name = {p["name"]: p for p in proposals}

    assert by_name["超出上限 rows[0].n - 400"]["request"]["body"]["rows"] == [{"n": 4}]


def test_dimension_quota_is_shared_across_recursion():
    """逐维限额必须跨递归共享：否则每下钻一层都会再放行 _MAX_PER_DIMENSION 条"""
    proposals = api_case_engine.generate_case_proposals(
        _body_endpoint(
            {
                "a": {"type": "integer", "maximum": 1},
                "b": {"type": "integer", "maximum": 1},
                "c": {"type": "integer", "maximum": 1},
                "nested": {
                    "type": "object",
                    "required": ["d"],
                    "properties": {"d": {"type": "integer", "maximum": 1}},
                },
            },
            ["a", "b", "c", "nested"],
        )
    )

    ranges = [p["name"] for p in proposals if p["name"].startswith("超出上限")]
    assert len(ranges) == api_case_engine._MAX_PER_DIMENSION  # 顶层已用满，嵌套字段不再生成


def test_deeply_nested_schema_is_bounded():
    """深嵌套 schema 不得递归爆栈：下钻受 _MAX_ANOMALY_DEPTH 约束"""
    schema = {"type": "string", "maxLength": 3}
    for _ in range(12):
        schema = {"type": "object", "required": ["child"], "properties": {"child": schema}}

    proposals = api_case_engine.generate_case_proposals(_body_endpoint({"root": schema}, ["root"]))

    assert proposals  # 正常请求 + 缺失必填仍在，只是不再继续下钻
    assert all(p["name"] in ("正常请求", "缺失必填字段 - 400") for p in proposals)
