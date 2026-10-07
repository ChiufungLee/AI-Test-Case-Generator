"""API 规格资产服务：OpenAPI 导入解析（yaml/json 自适应 + 局部 $ref 解引用）、URL 导入与同步、CRUD、接口清单"""

import json
import logging
from urllib.parse import urlsplit

import httpx
import yaml
from sqlalchemy import func, or_

from config import get_api_spec_import_timeout
from models.api_test_models import ApiEndpoint, ApiEndpointCase, ApiSpec
from models.database import create_session
from models.user import User

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """资源不存在或不可见（端点转 404）"""


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
_MAX_REF_DEPTH = 8
_MAX_CONTENT_LENGTH = 2_000_000
# 请求体媒体类型按此优先级取第一个命中（D-024）
_BODY_MEDIA_TYPES = ("application/json", "application/x-www-form-urlencoded", "multipart/form-data")


# ---------- 解析 ----------


def parse_openapi(content: str, format: str | None = None) -> tuple[dict, list[dict]]:
    """解析 OpenAPI 文档 → (spec 信息 dict, 端点行 list)；结构非法抛 ValueError。

    format 为空时自适应（先按 JSON 尝试，失败按 YAML）；端点行已做局部 $ref 解引用。
    """
    if len(content) > _MAX_CONTENT_LENGTH:
        raise ValueError("文档过大（上限 2MB）")
    doc = _load_document(content, format)
    if not isinstance(doc, dict):
        raise ValueError("OpenAPI 文档顶层必须是对象")

    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("文档缺少 paths 定义（或为空）")

    endpoints = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params = [p for p in (path_item.get("parameters") or []) if isinstance(p, dict)]
        for method, operation in path_item.items():
            if str(method).lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = _merge_parameters(shared_params, operation.get("parameters") or [])
            parameters = [_deref_schema(p, doc) for p in parameters]
            request_body, media_type = _request_body_schema(operation, doc)
            # OpenAPI 2.0 兼容：in: body 参数提升为 requestBody、in: formData 参数聚合为表单请求体（两种版本统一输出）
            body_params = [p for p in parameters if p.get("in") == "body" and isinstance(p.get("schema"), dict)]
            if body_params and not request_body:
                request_body = body_params[0]["schema"]
                media_type = "application/json"
                parameters = [p for p in parameters if p.get("in") != "body"]
            form_params = [p for p in parameters if p.get("in") == "formData" and p.get("name")]
            if form_params and not request_body:
                request_body = _form_params_schema(form_params)
                media_type = _form_media_type(operation, form_params)
                parameters = [p for p in parameters if p.get("in") != "formData"]
            responses = operation.get("responses")
            endpoints.append({
                "method": str(method).lower(),
                "path": str(path),
                "operation_id": str(operation.get("operationId") or ""),
                "summary": str(operation.get("summary") or operation.get("description") or "")[:500],
                "parameters": parameters,
                "request_body": request_body,
                "request_body_media_type": media_type,
                "responses": {
                    str(code): str(item.get("description") or "")
                    for code, item in (responses or {}).items()
                    if isinstance(item, dict)
                },
                "response_schemas": _response_schemas(operation, doc),
            })
    if not endpoints:
        raise ValueError("文档中没有可识别的接口操作")

    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    spec_info = {
        "title": str(info.get("title") or "")[:200],
        "version": str(info.get("version") or "")[:50],
    }
    return spec_info, endpoints


def _load_document(content: str, format: str | None) -> object:
    if format not in (None, "yaml", "json"):
        raise ValueError(f"不支持的文档格式: {format}（仅支持 yaml/json）")
    if format == "yaml":
        return _parse_yaml(content)
    if format == "json":
        return _parse_json(content)
    # 自适应：JSON 解析失败再按 YAML（YAML 是 JSON 超集，但显式先 JSON 可给出更准确的报错）
    try:
        return _parse_json(content)
    except ValueError:
        return _parse_yaml(content)


def _parse_json(content: str) -> object:
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError("文档不是合法 JSON") from e


def _parse_yaml(content: str) -> object:
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError("文档不是合法 YAML") from e


def _merge_parameters(shared: list[dict], operation_params: list) -> list[dict]:
    """path 级与操作级 parameters 合并，同名同位置的以操作级覆盖"""
    merged = list(shared)
    seen = {(p.get("name"), p.get("in")) for p in merged}
    for param in operation_params:
        if not isinstance(param, dict):
            continue
        key = (param.get("name"), param.get("in"))
        if key in seen:
            merged = [m for m in merged if (m.get("name"), m.get("in")) != key]
        merged.append(param)
        seen.add(key)
    return merged


def _request_body_schema(operation: dict, doc: dict) -> tuple[dict | str, str]:
    """提取 requestBody 的 JSON Schema 快照与媒体类型（D-024）。

    媒体类型按 _BODY_MEDIA_TYPES 优先级取第一个命中（json > urlencoded > multipart，
    覆盖 FastAPI 的 Form/File 端点）；无请求体或无可识别媒体类型返回 ("", "")。
    """
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return "", ""
    content = body.get("content")
    if not isinstance(content, dict):
        return "", ""
    media_type = next((m for m in _BODY_MEDIA_TYPES if m in content), None)
    if media_type is None:
        return "", ""
    media = content.get(media_type)
    schema = media.get("schema") if isinstance(media, dict) else None
    if not isinstance(schema, dict):
        return "", ""
    return _deref_schema(schema, doc), media_type


_JSON_MEDIA_TYPES = ("application/json", "text/json")


def _response_schemas(operation: dict, doc: dict) -> dict:
    """提取响应体的 JSON Schema 快照（D-027）：{状态码: schema}。

    3.0 取 responses[code].content 的首个 JSON 型媒体类型（application/json、text/json、*+json），
    2.0 取 responses[code].schema；$ref 做文档内解引用；无 schema 的状态码不收录。
    """
    schemas = {}
    for code, item in (operation.get("responses") or {}).items():
        if not isinstance(item, dict):
            continue
        schema = None
        content = item.get("content")
        if isinstance(content, dict):
            media_type = next(
                (m for m in content if m in _JSON_MEDIA_TYPES or str(m).endswith("+json")),
                None,
            )
            media = content.get(media_type) if media_type else None
            schema = media.get("schema") if isinstance(media, dict) else None
        elif "schema" in item:  # Swagger 2.0：响应直接挂 schema
            schema = item.get("schema")
        if isinstance(schema, dict):
            schemas[str(code)] = _deref_schema(schema, doc)
    return schemas


def _form_params_schema(form_params: list[dict]) -> dict:
    """Swagger 2.0 in:formData 参数聚合为对象 schema（type/format/enum 等约束原样保留）"""
    properties = {
        p["name"]: {k: v for k, v in p.items() if k not in ("name", "in", "required", "description")}
        for p in form_params
    }
    return {
        "type": "object",
        "properties": properties,
        "required": [p["name"] for p in form_params if p.get("required")],
    }


def _form_media_type(operation: dict, form_params: list[dict]) -> str:
    """formData 的媒体类型：consumes 声明优先，否则含 file 字段为 multipart、纯字段为 urlencoded"""
    consumes = operation.get("consumes") or []
    if "multipart/form-data" in consumes:
        return "multipart/form-data"
    if "application/x-www-form-urlencoded" in consumes:
        return "application/x-www-form-urlencoded"
    has_file = any(p.get("type") == "file" for p in form_params)
    return "multipart/form-data" if has_file else "application/x-www-form-urlencoded"


def _resolve_ref(ref: str, doc: dict, depth: int) -> dict | None:
    """解析文档内局部 $ref（#/components/schemas/X 或 #/definitions/X）；外部引用或越界返回 None"""
    if depth > _MAX_REF_DEPTH or not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _deref_schema(schema, doc: dict, depth: int = 0):
    """递归解引用局部 $ref；外部引用保留原样；循环引用超深度时截断为 {}（快照自包含的代价）"""
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        target = _resolve_ref(schema["$ref"], doc, depth)
        if target is None:
            return schema
        return _deref_schema(target, doc, depth + 1)
    resolved = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            resolved[key] = _deref_schema(value, doc, depth + 1)
        elif isinstance(value, list):
            resolved[key] = [
                _deref_schema(item, doc, depth + 1) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            resolved[key] = value
    return resolved


# ---------- 载荷（供端点直接返回的 dict） ----------


def spec_payload(spec: ApiSpec, owner_username: str | None = None, is_mine: bool = False) -> dict:
    return {
        "id": spec.id,
        "name": spec.name,
        "description": spec.description or "",
        "format": spec.format,
        "spec_title": spec.spec_title,
        "spec_version": spec.spec_version,
        "endpoint_count": spec.endpoint_count,
        "visibility": spec.visibility,
        "owner_user_id": spec.owner_user_id,
        "owner_username": owner_username,
        "is_mine": is_mine,
        "source_url": spec.source_url,
        "auth": _auth_payload(_parse_auth_config(spec.auth_config_json), include_body=is_mine),
        "created_at": spec.created_at,
        "updated_at": spec.updated_at,
    }


def endpoint_payload(endpoint: ApiEndpoint) -> dict:
    try:
        parameters = json.loads(endpoint.parameters_json)
    except (TypeError, ValueError):
        parameters = []
    try:
        request_body = json.loads(endpoint.request_body_json) if endpoint.request_body_json else None
    except (TypeError, ValueError):
        request_body = None
    try:
        responses = json.loads(endpoint.responses_json)
    except (TypeError, ValueError):
        responses = {}
    try:
        response_schemas = json.loads(endpoint.response_schemas_json) if endpoint.response_schemas_json else {}
    except (TypeError, ValueError):
        response_schemas = {}
    return {
        "id": endpoint.id,
        "method": endpoint.method,
        "path": endpoint.path,
        "operation_id": endpoint.operation_id,
        "summary": endpoint.summary,
        "parameters": parameters,
        "request_body": request_body,
        "request_body_media_type": endpoint.request_body_media_type or "",
        "responses": responses,
        "response_schemas": response_schemas,
    }


def get_username(user_id: int) -> str | None:
    db = create_session()
    try:
        return db.query(User.username).filter(User.id == user_id).scalar()
    finally:
        db.close()


# ---------- CRUD ----------


def create_api_spec(
    user_id: int,
    name: str,
    content: str,
    format: str | None = None,
    source_url: str | None = None,
    description: str | None = None,
) -> ApiSpec:
    """导入 OpenAPI 文档：解析 + 落资产与接口快照；结构非法抛 ValueError（端点转 422）。

    source_url 非 None 时为 URL 导入（供后续「同步」重新拉取）。
    """
    spec_info, endpoint_rows = parse_openapi(content, format)
    detected_format = format or _detect_format(content)
    db = create_session()
    try:
        spec = ApiSpec(
            owner_user_id=user_id,
            name=name,
            description=(description or "").strip(),
            format=detected_format,
            content=content,
            source_url=source_url,
            spec_title=spec_info["title"],
            spec_version=spec_info["version"],
            endpoint_count=len(endpoint_rows),
        )
        db.add(spec)
        db.flush()
        db.add_all([
            ApiEndpoint(
                spec_id=spec.id,
                method=row["method"],
                path=row["path"],
                operation_id=row["operation_id"],
                summary=row["summary"],
                parameters_json=json.dumps(row["parameters"], ensure_ascii=False),
                request_body_json=json.dumps(row["request_body"], ensure_ascii=False) if row["request_body"] else "",
                request_body_media_type=row["request_body_media_type"],
                responses_json=json.dumps(row["responses"], ensure_ascii=False),
                response_schemas_json=(
                    json.dumps(row["response_schemas"], ensure_ascii=False)
                    if row["response_schemas"] else ""
                ),
            )
            for row in endpoint_rows
        ])
        db.commit()
        db.refresh(spec)
        logger.info("用户 %s 导入 API 规格 %s（%s 个接口）", user_id, spec.id, spec.endpoint_count)
        return spec
    except Exception as e:
        db.rollback()
        logger.error("导入 API 规格失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


# ---------- 登录态前置请求配置（D-025） ----------


def _parse_auth_config(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        config = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(config, dict) or not config.get("path"):
        return None
    return config


def _auth_payload(config: dict | None, include_body: bool) -> dict | None:
    """登录态配置对外载荷：方法/路径/类型所有人可见，请求体（含凭据）仅 owner 可见"""
    if config is None:
        return None
    return {
        "method": str(config.get("method") or "post"),
        "path": str(config.get("path") or ""),
        "body_type": str(config.get("body_type") or "json"),
        "token_field": config.get("token_field") or None,
        "body": (config.get("body") or {}) if include_body else None,
    }


def update_api_spec_meta(spec_id: str, user_id: int, name: str | None = None, description: str | None = None) -> ApiSpec:
    """编辑接口文档名称/描述（owner-only，卡片「编辑」入口）；至少提供一项，否则 422"""
    if name is None and description is None:
        raise ValueError("请提供要修改的名称或描述")
    if name is not None and not name.strip():
        raise ValueError("名称不能为空")
    db = create_session()
    try:
        spec = (
            db.query(ApiSpec)
            .filter(ApiSpec.id == spec_id, ApiSpec.owner_user_id == user_id)
            .first()
        )
        if spec is None:
            raise NotFoundError("接口文档不存在")
        if name is not None:
            spec.name = name.strip()
        if description is not None:
            spec.description = description.strip()
        db.commit()
        db.refresh(spec)
        logger.info("用户 %s 编辑接口文档 %s 元信息", user_id, spec_id)
        return spec
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def set_auth_config(spec_id: str, user_id: int, auth: dict | None) -> dict | None:
    """设置/清除登录态前置请求配置（owner-only，D-025）；返回完整配置（含请求体）。

    auth 为 None 表示清除；path 必须以 / 开头，否则抛 ValueError（端点转 422）。
    """
    if auth is not None:
        path = str(auth.get("path") or "")
        if not path.startswith("/"):
            raise ValueError("登录接口路径必须以 / 开头")
    db = create_session()
    try:
        spec = (
            db.query(ApiSpec)
            .filter(ApiSpec.id == spec_id, ApiSpec.owner_user_id == user_id)
            .first()
        )
        if spec is None:
            raise NotFoundError("接口文档不存在")
        if auth is None:
            spec.auth_config_json = ""
        else:
            spec.auth_config_json = json.dumps({
                "method": str(auth.get("method") or "post"),
                "path": path,
                "body": auth.get("body") or {},
                "body_type": str(auth.get("body_type") or "json"),
                "token_field": auth.get("token_field") or None,
            }, ensure_ascii=False)
        db.commit()
        db.refresh(spec)
        logger.info("用户 %s %s 接口文档 %s 的登录态配置", user_id, "清除" if auth is None else "设置", spec_id)
        return _auth_payload(_parse_auth_config(spec.auth_config_json), include_body=True)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_auth_config(spec_id: str) -> dict | None:
    """执行器用：读取登录态配置（完整，含凭据；仅服务端内部使用）"""
    db = create_session()
    try:
        spec = db.query(ApiSpec).filter(ApiSpec.id == spec_id).first()
        return _parse_auth_config(spec.auth_config_json) if spec else None
    finally:
        db.close()


def _detect_format(content: str) -> str:
    stripped = content.lstrip()
    return "json" if stripped.startswith("{") else "yaml"


# ---------- URL 导入与同步（D-022） ----------


def fetch_openapi_document(url: str) -> str:
    """从 URL 拉取 OpenAPI 文档原文（模块级函数，测试经 monkeypatch 替换打桩）。

    仅 http/https、跟随重定向、上限 2MB（与粘贴导入一致）、超时 API_SPEC_IMPORT_TIMEOUT；
    拉取失败/超限/非 UTF-8 抛 ValueError（端点转 422）。内部测试平台不设内网限制，
    与执行阶段允许任意 base_url 的语义一致。
    """
    if not str(url).startswith(("http://", "https://")):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    chunks: list[bytes] = []
    total = 0
    try:
        with httpx.Client(follow_redirects=True, timeout=get_api_spec_import_timeout()) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise ValueError(f"拉取失败：HTTP {response.status_code}")
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_CONTENT_LENGTH:
                        raise ValueError("文档过大（上限 2MB）")
                    chunks.append(chunk)
    except httpx.HTTPError as e:
        raise ValueError(f"拉取失败：{e}") from e
    try:
        # utf-8-sig 兼容带 BOM 文档，普通 UTF-8 不受影响
        return b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError("文档不是 UTF-8 编码") from e


def create_api_spec_from_url(
    user_id: int, url: str, name: str | None = None, description: str | None = None
) -> ApiSpec:
    """从 URL 导入：拉取 → 解析 → 落资产；名称缺省取文档 info.title，其次主机名"""
    content = fetch_openapi_document(url)
    spec_info, _endpoints = parse_openapi(content)  # 结构非法在此抛 ValueError
    if not name or not name.strip():
        name = spec_info["title"] or urlsplit(url).netloc
    return create_api_spec(user_id, name[:200], content, None, source_url=str(url), description=description)


def _refresh_endpoint_snapshots(db, spec: ApiSpec, spec_info: dict, endpoint_rows: list[dict]) -> None:
    """按 (method, path) 匹配原行更新接口快照（D-022）：端点 id 不变 → 既有用例与执行历史关联保留；
    内容中已消失的接口连及其用例删除。同步与重新解析（D-029）共用。"""
    existing = db.query(ApiEndpoint).filter(ApiEndpoint.spec_id == spec.id).all()
    by_key = {(e.method, e.path): e for e in existing}
    seen_keys = set()
    for row in endpoint_rows:
        key = (row["method"], row["path"])
        if key in by_key:
            endpoint = by_key[key]
            endpoint.operation_id = row["operation_id"]
            endpoint.summary = row["summary"]
            endpoint.parameters_json = json.dumps(row["parameters"], ensure_ascii=False)
            endpoint.request_body_json = (
                json.dumps(row["request_body"], ensure_ascii=False) if row["request_body"] else ""
            )
            endpoint.request_body_media_type = row["request_body_media_type"]
            endpoint.responses_json = json.dumps(row["responses"], ensure_ascii=False)
            endpoint.response_schemas_json = (
                json.dumps(row["response_schemas"], ensure_ascii=False) if row["response_schemas"] else ""
            )
        else:
            db.add(ApiEndpoint(
                spec_id=spec.id,
                method=row["method"],
                path=row["path"],
                operation_id=row["operation_id"],
                summary=row["summary"],
                parameters_json=json.dumps(row["parameters"], ensure_ascii=False),
                request_body_json=(
                    json.dumps(row["request_body"], ensure_ascii=False) if row["request_body"] else ""
                ),
                request_body_media_type=row["request_body_media_type"],
                responses_json=json.dumps(row["responses"], ensure_ascii=False),
                response_schemas_json=(
                    json.dumps(row["response_schemas"], ensure_ascii=False) if row["response_schemas"] else ""
                ),
            ))
        seen_keys.add(key)
    for key, endpoint in by_key.items():
        if key not in seen_keys:
            db.delete(endpoint)
    spec.spec_title = spec_info["title"]
    spec.spec_version = spec_info["version"]
    spec.endpoint_count = len(endpoint_rows)


def reparse_api_spec(spec_id: str, user_id: int) -> ApiSpec:
    """重新解析已存储的文档并按 (method, path) 刷新接口快照（保留既有用例，D-029）。

    粘贴导入文档的「同步」等价物：不拉取远端、不改写 content，仅用当前解析器重放存量内容
    ——解析器升级后旧文档可自愈。owner-only；文档不存在或不可见抛 NotFoundError；
    存量内容解析失败抛 ValueError（端点转 422）。
    """
    db = create_session()
    try:
        spec = db.query(ApiSpec).filter(ApiSpec.id == spec_id, ApiSpec.owner_user_id == user_id).first()
        if spec is None:
            raise NotFoundError("接口文档不存在")
        spec_info, endpoint_rows = parse_openapi(spec.content, spec.format)
        _refresh_endpoint_snapshots(db, spec, spec_info, endpoint_rows)
        db.commit()
        db.refresh(spec)
        logger.info("用户 %s 重新解析接口文档 %s（%s 个接口）", user_id, spec.id, spec.endpoint_count)
        return spec
    except (NotFoundError, ValueError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("重新解析接口文档失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def sync_api_spec(spec_id: str, user_id: int) -> ApiSpec:
    """同步 URL 导入的规格：重新拉取文档，按 (method, path) 匹配刷新接口快照（保留既有用例，D-022）。

    owner-only（覆盖 content 与接口快照）；无 source_url / 拉取或解析失败抛 ValueError；
    规格不可见抛 NotFoundError。远端已消失的接口连及其用例一并删除。
    """
    db = create_session()
    try:
        spec = db.query(ApiSpec).filter(ApiSpec.id == spec_id, ApiSpec.owner_user_id == user_id).first()
        if spec is None:
            raise NotFoundError("接口文档不存在")
        source_url = spec.source_url
    finally:
        db.close()

    if not source_url:
        raise ValueError("该文档不是从 URL 导入的，无法同步")
    content = fetch_openapi_document(source_url)
    spec_info, endpoint_rows = parse_openapi(content)

    db = create_session()
    try:
        spec = db.query(ApiSpec).filter(ApiSpec.id == spec_id, ApiSpec.owner_user_id == user_id).first()
        if spec is None:
            raise NotFoundError("接口文档不存在")
        spec.content = content
        spec.format = _detect_format(content)
        _refresh_endpoint_snapshots(db, spec, spec_info, endpoint_rows)
        db.commit()
        db.refresh(spec)
        logger.info("用户 %s 同步接口文档 %s（%s 个接口）", user_id, spec.id, spec.endpoint_count)
        return spec
    except (NotFoundError, ValueError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("同步接口文档失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def list_api_specs(user_id: int) -> list[dict]:
    """当前用户可见的规格（自己的 + 共享的），附创建者用户名"""
    db = create_session()
    try:
        rows = (
            db.query(ApiSpec, User.username)
            .join(User, ApiSpec.owner_user_id == User.id)
            .filter(or_(ApiSpec.owner_user_id == user_id, ApiSpec.visibility == "shared"))
            .order_by(ApiSpec.updated_at.desc())
            .all()
        )
        return [
            {**spec_payload(spec, owner_username=username, is_mine=spec.owner_user_id == user_id)}
            for spec, username in rows
        ]
    finally:
        db.close()


def _get_readable_spec(db, spec_id: str, user_id: int, allow_shared_read: bool = True) -> ApiSpec | None:
    query = db.query(ApiSpec).filter(ApiSpec.id == spec_id)
    if allow_shared_read:
        query = query.filter(or_(ApiSpec.owner_user_id == user_id, ApiSpec.visibility == "shared"))
    else:
        query = query.filter(ApiSpec.owner_user_id == user_id)
    return query.first()


def get_api_spec(spec_id: str, user_id: int, allow_shared_read: bool = True) -> ApiSpec | None:
    """获取可见的规格；不可见返回 None（404 语义）"""
    db = create_session()
    try:
        return _get_readable_spec(db, spec_id, user_id, allow_shared_read)
    finally:
        db.close()


def get_api_spec_view(spec_id: str, user_id: int) -> dict | None:
    """详情视图：规格 + 创建者用户名 + 接口清单"""
    db = create_session()
    try:
        row = (
            db.query(ApiSpec, User.username)
            .join(User, ApiSpec.owner_user_id == User.id)
            .filter(ApiSpec.id == spec_id)
            .filter(or_(ApiSpec.owner_user_id == user_id, ApiSpec.visibility == "shared"))
            .first()
        )
        if not row:
            return None
        spec, username = row
        endpoints = (
            db.query(ApiEndpoint)
            .filter(ApiEndpoint.spec_id == spec.id)
            .order_by(ApiEndpoint.path.asc(), ApiEndpoint.method.asc())
            .all()
        )
        # 接口 → 用例数映射：清单页展示 + 执行范围提示判断「尚无用例将跳过」（批量执行 UX，D-027 配套）
        case_counts = {
            endpoint_id: count
            for endpoint_id, count in (
                db.query(ApiEndpointCase.endpoint_id, func.count(ApiEndpointCase.id))
                .join(ApiEndpoint, ApiEndpointCase.endpoint_id == ApiEndpoint.id)
                .filter(ApiEndpoint.spec_id == spec.id)
                .group_by(ApiEndpointCase.endpoint_id)
                .all()
            )
        }
        payload = spec_payload(spec, owner_username=username, is_mine=spec.owner_user_id == user_id)
        payload["endpoints"] = [
            {**endpoint_payload(endpoint), "case_count": case_counts.get(endpoint.id, 0)}
            for endpoint in endpoints
        ]
        return payload
    finally:
        db.close()


def list_endpoints(spec_id: str) -> list[ApiEndpoint]:
    db = create_session()
    try:
        return (
            db.query(ApiEndpoint)
            .filter(ApiEndpoint.spec_id == spec_id)
            .order_by(ApiEndpoint.path.asc(), ApiEndpoint.method.asc())
            .all()
        )
    finally:
        db.close()


def get_endpoint(spec_id: str, endpoint_id: str) -> ApiEndpoint | None:
    db = create_session()
    try:
        return (
            db.query(ApiEndpoint)
            .filter(ApiEndpoint.spec_id == spec_id, ApiEndpoint.id == endpoint_id)
            .first()
        )
    finally:
        db.close()


def delete_api_spec(spec_id: str, user_id: int) -> None:
    """删除规格（接口级联），owner-only"""
    db = create_session()
    try:
        spec = db.query(ApiSpec).filter(ApiSpec.id == spec_id).first()
        if not spec or spec.owner_user_id != user_id:
            raise NotFoundError("接口文档不存在")
        db.delete(spec)
        db.commit()
        logger.info("API 规格 %s 已删除", spec_id)
    finally:
        db.close()
