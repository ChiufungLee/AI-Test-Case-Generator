"""API 规格资产：OpenAPI 解析服务与端点测试"""

import json

import pytest

from models.api_test_models import ApiEndpointCase
from models.user import User
from services import api_spec_service


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


SPEC_V3_YAML = """openapi: 3.0.0
info:
  title: 用户服务
  version: 1.0.0
paths:
  /users:
    post:
      operationId: createUser
      summary: 创建用户
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateUser'
      responses:
        '201':
          description: created
  /users/{id}:
    get:
      operationId: getUser
      summary: 查询用户
      parameters:
        - name: verbose
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: ok
    parameters:
      - name: id
        in: path
        required: true
        schema:
          type: integer
          minimum: 1
components:
  schemas:
    CreateUser:
      type: object
      required: [username, password]
      properties:
        username:
          type: string
          minLength: 3
          maxLength: 20
        password:
          type: string
          minLength: 8
"""

SPEC_V2_YAML = """swagger: '2.0'
info:
  title: 旧版服务
  version: '2.0'
paths:
  /pets:
    post:
      summary: 创建宠物
      parameters:
        - name: body
          in: body
          required: true
          schema:
            $ref: '#/definitions/Pet'
      responses:
        '200':
          description: ok
definitions:
  Pet:
    type: object
    required: [name]
    properties:
      name:
        type: string
"""

SPEC_JSON = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "用户服务", "version": "1.0.0"},
    "paths": {
        "/ping": {
            "get": {"operationId": "ping", "summary": "健康检查", "responses": {"200": {"description": "ok"}}}
        }
    },
}, ensure_ascii=False)


# ---------- 解析（服务层） ----------


def test_parse_yaml_v3_extracts_operations_and_derefs_refs():
    spec_info, endpoints = api_spec_service.parse_openapi(SPEC_V3_YAML)

    assert spec_info == {"title": "用户服务", "version": "1.0.0"}
    assert [(e["method"], e["path"]) for e in endpoints] == [
        ("post", "/users"),
        ("get", "/users/{id}"),
    ]

    post = endpoints[0]
    # $ref 已解引用：requestBody 快照内是具体 schema 而非引用
    assert post["request_body"]["required"] == ["username", "password"]
    assert post["request_body"]["properties"]["username"]["minLength"] == 3

    get_detail = endpoints[1]
    # path 级参数与操作级参数合并（path 级在前，操作级追加）
    assert [p["name"] for p in get_detail["parameters"]] == ["id", "verbose"]
    assert get_detail["parameters"][1]["schema"].get("type") == "boolean"


def test_parse_yaml_v2_swagger_derefs_definitions():
    spec_info, endpoints = api_spec_service.parse_openapi(SPEC_V2_YAML)

    assert spec_info == {"title": "旧版服务", "version": "2.0"}
    assert len(endpoints) == 1
    body = endpoints[0]["request_body"]
    assert body["required"] == ["name"]
    assert body["properties"]["name"]["type"] == "string"


def test_parse_json_and_auto_detect():
    spec_info, endpoints = api_spec_service.parse_openapi(SPEC_JSON, "json")
    assert len(endpoints) == 1 and endpoints[0]["path"] == "/ping"

    spec_info2, _ = api_spec_service.parse_openapi(SPEC_JSON)  # 不传 format 自适应
    assert spec_info2["title"] == "用户服务"


def test_parse_invalid_documents_raise_value_error():
    with pytest.raises(ValueError):
        api_spec_service.parse_openapi("::: 不是合法 yaml ::: [", "yaml")
    with pytest.raises(ValueError):
        api_spec_service.parse_openapi('{"openapi": "3.0.0", "info": {}}')  # 缺 paths
    with pytest.raises(ValueError):
        api_spec_service.parse_openapi('{"paths": {}}')  # paths 为空
    with pytest.raises(ValueError):
        api_spec_service.parse_openapi('{"paths": {"/x": {"summary": "只有文档字段"}}}')  # 无操作
    with pytest.raises(ValueError):
        api_spec_service.parse_openapi("[]")  # 顶层非对象
    with pytest.raises(ValueError):
        api_spec_service.parse_openapi(SPEC_JSON, "xml")  # 未知格式


def test_parse_ignores_non_operation_keys_and_methods():
    doc = json.dumps({
        "openapi": "3.0.0",
        "paths": {
            "/x": {
                "parameters": [{"name": "shared", "in": "query", "schema": {"type": "string"}}],
                "x-internal": {"foo": "bar"},
                "trace": {"summary": "不支持的 method 不提取"},
                "get": {"summary": "有效操作", "responses": {"200": {"description": "ok"}}},
            }
        },
    })
    _, endpoints = api_spec_service.parse_openapi(doc, "json")
    assert [(e["method"], e["path"]) for e in endpoints] == [("get", "/x")]
    assert endpoints[0]["parameters"][0]["name"] == "shared"


def test_create_api_spec_persists_endpoints(db_session, make_user):
    make_user("alice", "secret123")
    alice = db_session.query(User).filter(User.username == "alice").first().id

    spec = api_spec_service.create_api_spec(alice, "用户服务", SPEC_V3_YAML, "yaml")

    assert spec.endpoint_count == 2
    assert spec.spec_title == "用户服务"
    assert spec.format == "yaml"
    endpoints = api_spec_service.list_endpoints(spec.id)
    assert len(endpoints) == 2
    post = next(e for e in endpoints if e.method == "post")
    assert "username" in json.loads(post.request_body_json)["properties"]


def test_get_api_spec_view_visibility(db_session, make_user):
    make_user("alice", "secret123")
    make_user("bob", "secret123")
    alice = db_session.query(User).filter(User.username == "alice").first().id
    bob = db_session.query(User).filter(User.username == "bob").first().id

    mine = api_spec_service.create_api_spec(alice, "我的规格", SPEC_JSON, "json")
    shared = api_spec_service.create_api_spec(bob, "共享规格", SPEC_JSON, "json")
    from models.api_test_models import ApiSpec as ApiSpecModel

    shared_row = db_session.query(ApiSpecModel).filter(ApiSpecModel.id == shared.id).first()
    shared_row.visibility = "shared"
    db_session.commit()

    assert api_spec_service.get_api_spec(mine.id, alice) is not None
    assert api_spec_service.get_api_spec(mine.id, bob) is None  # 他人 private 不可见
    assert api_spec_service.get_api_spec(shared.id, alice) is not None  # 共享可读

    view = api_spec_service.get_api_spec_view(shared.id, alice)
    assert view["is_mine"] is False
    assert view["owner_username"] == "bob"
    assert len(view["endpoints"]) == 1


def test_delete_api_spec_owner_only(db_session, make_user):
    make_user("alice", "secret123")
    make_user("bob", "secret123")
    alice = db_session.query(User).filter(User.username == "alice").first().id
    bob = db_session.query(User).filter(User.username == "bob").first().id

    mine = api_spec_service.create_api_spec(alice, "我的规格", SPEC_JSON, "json")
    theirs = api_spec_service.create_api_spec(bob, "他人规格", SPEC_JSON, "json")

    with pytest.raises(api_spec_service.NotFoundError):
        api_spec_service.delete_api_spec(theirs.id, alice)  # 非 owner → 404 语义

    api_spec_service.delete_api_spec(mine.id, alice)
    assert api_spec_service.get_api_spec(mine.id, alice) is None
    assert api_spec_service.list_endpoints(mine.id) == []


# ---------- 端点 ----------


def test_api_spec_endpoints_require_login(client):
    assert client.post(
        "/api/api-specs", json={"name": "x", "content": SPEC_JSON, "format": "json"}
    ).status_code == 401
    assert client.get("/api/api-specs").status_code == 401


def test_api_spec_create_endpoint_happy_and_invalid(logged_in_client):
    response = logged_in_client.post(
        "/api/api-specs", json={"name": "用户服务", "content": SPEC_V3_YAML, "format": "yaml"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["endpoint_count"] == 2
    assert body["is_mine"] is True
    assert body["spec_title"] == "用户服务"

    bad = logged_in_client.post(
        "/api/api-specs", json={"name": "坏文档", "content": "{invalid json", "format": "json"}
    )
    assert bad.status_code == 422
    assert "JSON" in bad.json()["error"]


def test_api_spec_list_and_detail_visibility(logged_in_client, db_session, make_user, make_api_spec, make_api_endpoint):
    make_user("bob", "secret123")
    mine = make_api_spec(_alice_id(db_session), name="我的规格")
    shared = make_api_spec(_bob_id(db_session), name="共享规格", visibility="shared")
    make_api_spec(_bob_id(db_session), name="他人私有")
    make_api_endpoint(shared.id, method="get", path="/ping")

    names = {item["name"] for item in logged_in_client.get("/api/api-specs").json()}
    assert names == {"我的规格", "共享规格"}

    detail = logged_in_client.get(f"/api/api-specs/{shared.id}")
    assert detail.status_code == 200
    assert detail.json()["is_mine"] is False
    assert len(detail.json()["endpoints"]) == 1

    assert logged_in_client.get(f"/api/api-specs/{mine.id}").status_code == 200
    assert logged_in_client.get("/api/api-specs/nonexistent").status_code == 404


def test_api_spec_delete_endpoint_owner_only(logged_in_client, db_session, make_user, make_api_spec):
    make_user("bob", "secret123")
    mine = make_api_spec(_alice_id(db_session))
    bob_shared = make_api_spec(_bob_id(db_session), visibility="shared")

    assert logged_in_client.delete(f"/api/api-specs/{bob_shared.id}").status_code == 404

    response = logged_in_client.delete(f"/api/api-specs/{mine.id}")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert logged_in_client.get(f"/api/api-specs/{mine.id}").status_code == 404


# ---------- 请求体媒体类型（D-024：表单类接口不再丢失请求体） ----------


def test_parse_detects_form_media_types_v3():
    doc = json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "表单服务", "version": "1.0"},
        "paths": {
            "/login": {"post": {
                "requestBody": {"content": {"application/x-www-form-urlencoded": {"schema": {
                    "type": "object", "required": ["username", "password"],
                    "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
                }}}},
                "responses": {"200": {"description": "ok"}},
            }},
            "/upload": {"post": {
                "requestBody": {"content": {"multipart/form-data": {"schema": {
                    "type": "object", "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                }}}},
                "responses": {"200": {"description": "ok"}},
            }},
        },
    })
    _, endpoints = api_spec_service.parse_openapi(doc, "json")
    login = next(e for e in endpoints if e["path"] == "/login")
    upload = next(e for e in endpoints if e["path"] == "/upload")
    assert login["request_body_media_type"] == "application/x-www-form-urlencoded"
    assert login["request_body"]["required"] == ["username", "password"]
    assert upload["request_body_media_type"] == "multipart/form-data"
    assert upload["request_body"]["properties"]["file"]["format"] == "binary"


def test_parse_aggregates_v2_form_data_params():
    doc = """swagger: '2.0'
info:
  title: 旧版登录
  version: '1.0'
paths:
  /login:
    post:
      consumes:
        - application/x-www-form-urlencoded
      parameters:
        - name: username
          in: formData
          required: true
          type: string
        - name: password
          in: formData
          required: true
          type: string
      responses:
        '200':
          description: ok
"""
    _, endpoints = api_spec_service.parse_openapi(doc, "yaml")
    login = endpoints[0]
    # in: formData 参数聚合为表单请求体，从 parameters 中移除
    assert login["parameters"] == []
    assert login["request_body_media_type"] == "application/x-www-form-urlencoded"
    assert login["request_body"]["required"] == ["username", "password"]
    assert login["request_body"]["properties"]["username"]["type"] == "string"


def test_parse_v2_file_param_defaults_to_multipart():
    doc = """swagger: '2.0'
info:
  title: 旧版上传
  version: '1.0'
paths:
  /upload:
    post:
      parameters:
        - name: file
          in: formData
          required: true
          type: file
      responses:
        '200':
          description: ok
"""
    _, endpoints = api_spec_service.parse_openapi(doc, "yaml")
    assert endpoints[0]["request_body_media_type"] == "multipart/form-data"


def test_parse_json_body_still_wins_for_mixed_content():
    doc = json.dumps({
        "openapi": "3.0.0",
        "paths": {"/x": {"post": {
            "requestBody": {"content": {
                "application/json": {"schema": {"type": "object", "required": ["a"]}},
                "multipart/form-data": {"schema": {"type": "object"}},
            }},
            "responses": {"200": {"description": "ok"}},
        }}},
    })
    _, endpoints = api_spec_service.parse_openapi(doc, "json")
    assert endpoints[0]["request_body_media_type"] == "application/json"


# ---------- URL 导入与同步（D-022） ----------


def test_import_from_url_endpoint_creates_spec_with_source(logged_in_client, monkeypatch):
    monkeypatch.setattr(api_spec_service, "fetch_openapi_document", lambda url: SPEC_JSON)

    response = logged_in_client.post(
        "/api/api-specs/import-url", json={"url": "http://svc.example/openapi.json"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["endpoint_count"] == 1
    assert body["source_url"] == "http://svc.example/openapi.json"
    # 名称缺省取文档标题
    assert body["name"] == "用户服务"


def test_import_from_url_endpoint_maps_errors(logged_in_client, monkeypatch):
    def _boom(url):
        raise ValueError("拉取失败：HTTP 404")

    monkeypatch.setattr(api_spec_service, "fetch_openapi_document", _boom)
    response = logged_in_client.post("/api/api-specs/import-url", json={"url": "http://svc.example/x.json"})
    assert response.status_code == 422
    assert "404" in response.json()["error"]


def test_fetch_openapi_document_rejects_non_http_url():
    with pytest.raises(ValueError):
        api_spec_service.fetch_openapi_document("ftp://svc.example/x.json")


def test_sync_api_spec_matches_by_method_path_and_keeps_cases(
    db_session, make_user, make_api_spec, make_api_endpoint, make_api_endpoint_case, monkeypatch
):
    make_user("alice", "secret123")
    alice = db_session.query(User).filter(User.username == "alice").first().id
    spec = make_api_spec(alice, name="同步服务", source_url="http://svc.example/openapi.json")
    endpoint = make_api_endpoint(spec.id, method="get", path="/ping", summary="旧摘要")
    make_api_endpoint_case(endpoint.id, name="手工保留", request_json="{}", source_type="manual")
    make_api_endpoint(spec.id, method="get", path="/gone")  # 远端已消失的接口

    updated_doc = json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "同步服务", "version": "2.0.0"},
        "paths": {
            "/ping": {"get": {"summary": "新摘要", "responses": {"200": {"description": "ok"}}}},
            "/extra": {"get": {"summary": "新增接口", "responses": {"200": {"description": "ok"}}}},
        },
    })
    monkeypatch.setattr(api_spec_service, "fetch_openapi_document", lambda url: updated_doc)

    synced = api_spec_service.sync_api_spec(spec.id, alice)
    assert synced.endpoint_count == 2
    assert synced.spec_version == "2.0.0"
    assert synced.format == "json"

    endpoints = {e.path: e for e in api_spec_service.list_endpoints(spec.id)}
    assert set(endpoints) == {"/ping", "/extra"}  # 消失的 /gone 已删除
    assert endpoints["/ping"].id == endpoint.id  # 按 (method, path) 匹配原行
    assert endpoints["/ping"].summary == "新摘要"
    # 既有用例经原 endpoint 行保留
    cases = db_session.query(ApiEndpointCase).filter(ApiEndpointCase.endpoint_id == endpoint.id).all()
    assert [c.name for c in cases] == ["手工保留"]


def test_sync_api_spec_guards(db_session, make_user, make_api_spec, monkeypatch):
    make_user("alice", "secret123")
    make_user("bob", "secret123")
    alice = db_session.query(User).filter(User.username == "alice").first().id
    bob = db_session.query(User).filter(User.username == "bob").first().id

    no_url = make_api_spec(alice, name="粘贴导入")
    with pytest.raises(ValueError):
        api_spec_service.sync_api_spec(no_url.id, alice)  # 非 URL 导入不可同步

    theirs = make_api_spec(bob, name="他人文档", source_url="http://svc.example/openapi.json")
    with pytest.raises(api_spec_service.NotFoundError):
        api_spec_service.sync_api_spec(theirs.id, alice)  # owner-only


def test_sync_endpoint_error_mapping(logged_in_client, db_session, make_api_spec, monkeypatch):
    spec = make_api_spec(_alice_id(db_session), name="粘贴导入")
    response = logged_in_client.post(f"/api/api-specs/{spec.id}/sync")
    assert response.status_code == 422

    assert logged_in_client.post("/api/api-specs/nonexistent/sync").status_code == 404
