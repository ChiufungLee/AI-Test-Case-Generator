"""API 规格资产服务：OpenAPI 导入解析（yaml/json 自适应 + 局部 $ref 解引用）、CRUD、接口清单"""

import json
import logging

import yaml
from sqlalchemy import or_

from models.api_test_models import ApiEndpoint, ApiSpec
from models.database import create_session
from models.user import User

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """资源不存在或不可见（端点转 404）"""


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
_MAX_REF_DEPTH = 8
_MAX_CONTENT_LENGTH = 2_000_000


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
            request_body = _request_body_schema(operation, doc)
            # OpenAPI 2.0 兼容：in: body 参数提升为 requestBody（两种版本统一输出）
            body_params = [p for p in parameters if p.get("in") == "body" and isinstance(p.get("schema"), dict)]
            if body_params and not request_body:
                request_body = body_params[0]["schema"]
                parameters = [p for p in parameters if p.get("in") != "body"]
            endpoints.append({
                "method": str(method).lower(),
                "path": str(path),
                "operation_id": str(operation.get("operationId") or ""),
                "summary": str(operation.get("summary") or operation.get("description") or "")[:500],
                "parameters": parameters,
                "request_body": request_body,
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


def _request_body_schema(operation: dict, doc: dict) -> dict | str:
    """提取 requestBody 的 JSON Schema 快照（已解引用；无请求体或无 application/json 返回空串）"""
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    media = content.get("application/json") if isinstance(content, dict) else None
    schema = media.get("schema") if isinstance(media, dict) else None
    if not isinstance(schema, dict):
        return ""
    return _deref_schema(schema, doc)


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
        "format": spec.format,
        "spec_title": spec.spec_title,
        "spec_version": spec.spec_version,
        "endpoint_count": spec.endpoint_count,
        "visibility": spec.visibility,
        "owner_user_id": spec.owner_user_id,
        "owner_username": owner_username,
        "is_mine": is_mine,
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
    return {
        "id": endpoint.id,
        "method": endpoint.method,
        "path": endpoint.path,
        "operation_id": endpoint.operation_id,
        "summary": endpoint.summary,
        "parameters": parameters,
        "request_body": request_body,
    }


def get_username(user_id: int) -> str | None:
    db = create_session()
    try:
        return db.query(User.username).filter(User.id == user_id).scalar()
    finally:
        db.close()


# ---------- CRUD ----------


def create_api_spec(user_id: int, name: str, content: str, format: str | None = None) -> ApiSpec:
    """导入 OpenAPI 文档：解析 + 落资产与接口快照；结构非法抛 ValueError（端点转 422）"""
    spec_info, endpoint_rows = parse_openapi(content, format)
    detected_format = format or _detect_format(content)
    db = create_session()
    try:
        spec = ApiSpec(
            owner_user_id=user_id,
            name=name,
            format=detected_format,
            content=content,
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


def _detect_format(content: str) -> str:
    stripped = content.lstrip()
    return "json" if stripped.startswith("{") else "yaml"


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
        payload = spec_payload(spec, owner_username=username, is_mine=spec.owner_user_id == user_id)
        payload["endpoints"] = [endpoint_payload(endpoint) for endpoint in endpoints]
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
            raise NotFoundError("API 规格不存在")
        db.delete(spec)
        db.commit()
        logger.info("API 规格 %s 已删除", spec_id)
    finally:
        db.close()
