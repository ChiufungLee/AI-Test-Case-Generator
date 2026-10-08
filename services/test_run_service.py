"""测试执行服务：进程内 httpx 顺序执行接口用例（D-012），结果与事件经 RunHub 发布。

占用语义：同规格已有 running 运行时，本人转订阅（返回 None）、他人占用转 409（ConflictError）；
执行不绑定 HTTP 请求，客户端断开只影响 SSE 订阅。断言：状态码比对（D-021）+ 响应体字段断言
（D-027，求值于完整响应体、先于截断快照落库）。
"""

import asyncio
import json
import logging
import time
from urllib.parse import quote

import httpx
from sqlalchemy import func, or_

from config import get_api_test_timeout
from models.api_test_models import ApiEndpoint, ApiEndpointCase, ApiSpec, TestRun, TestRunResult
from models.database import create_session
from models.user import User
from services import api_assertions
from services import api_spec_service

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """资源不存在或不可见（端点转 404）"""


class ConflictError(Exception):
    """同规格已有他人执行中的运行（端点转 409）"""


_MAX_BODY_SNAPSHOT = 4096
_MAX_HEADER_SNAPSHOT = 20

# 请求快照脱敏：登录态派生的 Authorization/Cookie 等凭据不得落库或随结果下发
# （执行详情对规格可读者开放，D-023；明文凭据入库会跨用户泄漏）
_SENSITIVE_HEADERS = (
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
)


def _redact_headers(headers) -> dict:
    if not isinstance(headers, dict):
        return {}
    return {
        key: ("***" if str(key).lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


# 请求/响应体里按"键名精确命中"脱敏的凭据字段。只在顶层匹配、不按子串匹配：
# 避免误伤业务字段（如 token_count / password_reset_at）。
_SENSITIVE_BODY_KEYS = frozenset({
    "password", "passwd", "pwd", "token", "access_token", "refresh_token", "id_token",
    "secret", "client_secret", "app_secret", "authorization", "api_key", "apikey",
    "session", "sessionid", "cookie", "credential",
})


def _redact_body(body):
    """请求体快照脱敏：顶层键名命中已知凭据字段时打码"""
    if not isinstance(body, dict):
        return body
    return {
        key: ("***" if str(key).lower() in _SENSITIVE_BODY_KEYS and value not in (None, "") else value)
        for key, value in body.items()
    }


def _redact_json_body(text: str) -> str:
    """响应体快照脱敏：只在"完整且是 JSON 对象"时解析后打码。

    被 4KB 截断的 JSON 不解析——半截 JSON 重新序列化会破坏证据；
    字符串级正则替换同样会改坏原文，故不做。
    """
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(parsed, dict):
        return text
    redacted = _redact_body(parsed)
    if redacted == parsed:
        return text
    return json.dumps(redacted, ensure_ascii=False)


def _redact_assertion_actuals(results) -> list:
    """断言明细里的 actual 会回显响应值（如断言 data.token）——目标路径末段命中凭据键时打码"""
    if not isinstance(results, list):
        return []
    redacted = []
    for item in results:
        if not isinstance(item, dict):
            redacted.append(item)
            continue
        last_segment = str(item.get("target") or "").rsplit(".", 1)[-1].lower()
        if last_segment in _SENSITIVE_BODY_KEYS and item.get("actual") not in (None, ""):
            item = {**item, "actual": "***"}
        redacted.append(item)
    return redacted


def _create_run_client() -> httpx.AsyncClient:
    """每轮执行独立 AsyncClient（D-025）：登录态 Cookie 只在本轮的 cookie jar 内生效，
    不跨执行/跨用户串会话。测试经 monkeypatch 替换本函数为 MockTransport 客户端工厂。"""
    return httpx.AsyncClient(timeout=httpx.Timeout(get_api_test_timeout()))


# ---------- 占用与查询 ----------


def try_claim_run(spec_id: str, user_id: int, base_url: str, endpoint_ids: list[str] | None = None) -> TestRun | None:
    """占用式创建执行记录。

    返回新建的 TestRun（成功占用）；本人已有 running 运行返回 None（调用方转订阅既有运行）；
    他人占用中抛 ConflictError（端点转 409）。规格不可见抛 NotFoundError。
    """
    db = create_session()
    try:
        spec = (
            db.query(ApiSpec)
            .filter(ApiSpec.id == spec_id, ApiSpec.owner_user_id == user_id)
            .first()
        )
        if spec is None:
            raise NotFoundError("接口文档不存在")

        running = (
            db.query(TestRun)
            .filter(TestRun.spec_id == spec_id, TestRun.status == "running")
            .first()
        )
        if running is not None:
            if running.created_by == user_id:
                return None
            raise ConflictError("该文档已有其他用户执行中的运行")

        if not str(base_url).startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")

        case_rows = (
            db.query(ApiEndpoint, ApiEndpointCase)
            .join(ApiEndpointCase, ApiEndpointCase.endpoint_id == ApiEndpoint.id)
            .filter(ApiEndpoint.spec_id == spec_id, ApiEndpointCase.enabled == True)  # noqa: E712
        )
        if endpoint_ids:
            case_rows = case_rows.filter(ApiEndpoint.id.in_(endpoint_ids))
        case_rows = case_rows.all()

        endpoints_summary = []
        for endpoint_row, _case in case_rows:
            label = f"{endpoint_row.method.upper()} {endpoint_row.path}"
            if label not in endpoints_summary:
                endpoints_summary.append(label)

        run = TestRun(
            spec_id=spec_id,
            base_url=base_url,
            status="running",
            total=len(case_rows),
            endpoints_json=json.dumps(endpoints_summary, ensure_ascii=False),
            created_by=user_id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        logger.info("用户 %s 创建测试执行 %s（%s 条用例）", user_id, run.id, len(case_rows))
        return run
    except (NotFoundError, ConflictError):
        # 预期控制流（404/409 语义）：原样上抛，不打 ERROR 堆栈污染告警日志
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("创建测试执行失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def get_running_run(spec_id: str, user_id: int) -> TestRun | None:
    db = create_session()
    try:
        return (
            db.query(TestRun)
            .filter(TestRun.spec_id == spec_id, TestRun.status == "running", TestRun.created_by == user_id)
            .first()
        )
    finally:
        db.close()


def _run_visible(db, run: TestRun, user_id: int) -> bool:
    """执行记录可见性（D-023）：执行人本人，或其规格的可读者（owner/共享）"""
    if run.created_by == user_id:
        return True
    if not run.spec_id:
        return False
    spec = db.query(ApiSpec).filter(ApiSpec.id == run.spec_id).first()
    return spec is not None and (spec.owner_user_id == user_id or spec.visibility == "shared")


def get_run_view(run_id: str, user_id: int) -> dict | None:
    """执行详情（含逐条结果）；执行人或规格可读者可见（D-023），否则 404 语义"""
    db = create_session()
    try:
        row = (
            db.query(TestRun, User.username)
            .outerjoin(User, TestRun.created_by == User.id)
            .filter(TestRun.id == run_id)
            .first()
        )
        if row is None:
            return None
        run, username = row
        if not _run_visible(db, run, user_id):
            return None
        results = (
            db.query(TestRunResult)
            .filter(TestRunResult.run_id == run.id)
            .order_by(TestRunResult.created_at.asc(), TestRunResult.id.asc())
            .all()
        )
        # 一次查询建 endpoint id → "METHOD /path" 标签映射（endpoint 已删除则为 None）
        endpoint_ids = {r.endpoint_id for r in results if r.endpoint_id}
        labels = {}
        if endpoint_ids:
            rows = db.query(ApiEndpoint).filter(ApiEndpoint.id.in_(endpoint_ids)).all()
            labels = {e.id: f"{e.method.upper()} {e.path}" for e in rows}
        return {
            **run_payload(run, created_by_username=username),
            "results": [result_payload(r, endpoint_label=labels.get(r.endpoint_id)) for r in results],
        }
    finally:
        db.close()


def list_runs(spec_id: str, user_id: int) -> list[dict]:
    """规格的执行历史：规格可读者可见全部运行并展示执行人（D-023）；规格不可见抛 NotFoundError"""
    db = create_session()
    try:
        spec = db.query(ApiSpec).filter(ApiSpec.id == spec_id).first()
        if spec is None or not (spec.owner_user_id == user_id or spec.visibility == "shared"):
            raise NotFoundError("接口文档不存在")
        rows = (
            db.query(TestRun, User.username)
            .outerjoin(User, TestRun.created_by == User.id)
            .filter(TestRun.spec_id == spec_id)
            .order_by(TestRun.created_at.desc(), TestRun.id.desc())
            .limit(50)
            .all()
        )
        return [run_payload(run, created_by_username=username) for run, username in rows]
    finally:
        db.close()


def _parse_endpoints(raw: str | None) -> list:
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        parsed = []
    return parsed if isinstance(parsed, list) else []


def run_payload(run: TestRun, created_by_username: str | None = None) -> dict:
    return {
        "id": run.id,
        "spec_id": run.spec_id,
        "base_url": run.base_url,
        "status": run.status,
        "total": run.total,
        "passed": run.passed,
        "failed": run.failed,
        "errored": run.errored,
        "endpoints": _parse_endpoints(run.endpoints_json),
        "created_by": run.created_by,
        "created_by_username": created_by_username,
        "error": run.error,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


def result_payload(result: TestRunResult, endpoint_label: str | None = None) -> dict:
    try:
        request = json.loads(result.request_json)
    except (TypeError, ValueError):
        request = {}
    # 存量快照可能仍含明文凭据（写侧脱敏上线前落库），读取时兜底再脱敏一次
    if isinstance(request, dict):
        request["headers"] = _redact_headers(request.get("headers"))
        request["body"] = _redact_body(request.get("body"))
    try:
        response = json.loads(result.response_json)
    except (TypeError, ValueError):
        response = {}
    if isinstance(response, dict):
        response["headers"] = _redact_headers(response.get("headers"))
    try:
        assertions = json.loads(result.assertions_json) if result.assertions_json else []
    except (TypeError, ValueError):
        assertions = []
    return {
        "id": result.id,
        "endpoint": endpoint_label,
        "case_name": result.case_name,
        "request": request,
        "response": response,
        "verdict": result.verdict,
        "expected_status": result.expected_status,
        "actual_status": result.actual_status,
        "assertions": _redact_assertion_actuals(assertions) if isinstance(assertions, list) else [],
        "duration_ms": result.duration_ms,
        "failure_reason": result.failure_reason,
    }


# ---------- 执行 ----------


def _load_run_cases(spec_id: str, endpoint_ids: list[str] | None) -> list[tuple[ApiEndpoint, ApiEndpointCase]]:
    db = create_session()
    try:
        query = (
            db.query(ApiEndpoint, ApiEndpointCase)
            .join(ApiEndpointCase, ApiEndpointCase.endpoint_id == ApiEndpoint.id)
            .filter(ApiEndpoint.spec_id == spec_id, ApiEndpointCase.enabled == True)  # noqa: E712
        )
        if endpoint_ids:
            query = query.filter(ApiEndpoint.id.in_(endpoint_ids))
        return (
            query.order_by(ApiEndpoint.path.asc(), ApiEndpoint.method.asc(), ApiEndpointCase.created_at.asc())
            .all()
        )
    finally:
        db.close()


def _get_run(run_id: str) -> TestRun | None:
    db = create_session()
    try:
        return db.query(TestRun).filter(TestRun.id == run_id).first()
    finally:
        db.close()


def _split_form_fields(request_body_json: str | None, body: dict) -> tuple[dict, dict]:
    """multipart 请求体拆分（D-024）：schema 中 binary/type:file 字段进 files（占位文件），其余进 data"""
    try:
        schema = json.loads(request_body_json) if request_body_json else {}
    except (TypeError, ValueError):
        schema = {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    binary_fields = {
        name
        for name, sub in (properties or {}).items()
        if isinstance(sub, dict) and (sub.get("format") == "binary" or sub.get("type") == "file")
    }
    data: dict = {}
    files: dict = {}
    for key, value in body.items():
        if key in binary_fields:
            files[key] = (f"{key}.bin", b"test-file-content", "application/octet-stream")
        else:
            data[key] = value
    return data, files


def _invalid_request_shape(request) -> str | None:
    """用例 request 的形状校验：必须是对象，且 path/query/headers 若存在必须是对象。

    用例内容由规则引擎/AI/手工整表替换写入，落库时只校验顶层结构。形状非法时若继续
    .items()/.get() 会在**逐条 try 之外**抛异常，导致整轮执行中断、后续用例一条都不跑。
    """
    if not isinstance(request, dict):
        return "用例 request 必须是对象"
    for key in ("path", "query", "headers"):
        value = request.get(key)
        if value is not None and not isinstance(value, dict):
            return f"用例 request.{key} 必须是对象"
    return None


def _error_result(
    endpoint: ApiEndpoint,
    case: ApiEndpointCase,
    reason: str,
    *,
    url: str = "",
    query: dict | None = None,
    headers: dict | None = None,
    body=None,
    media_type: str = "",
    duration_ms: int = 0,
) -> dict:
    """构造 error 结论的单条结果（形状非法 / 网络异常共用同一种快照结构）"""
    return {
        "endpoint_id": endpoint.id,
        "case_name": case.name,
        "request_json": json.dumps(
            {
                "method": endpoint.method.upper(),
                "url": url,
                "query": query or {},
                "headers": _redact_headers(headers),
                "body": _redact_body(body),
                "media_type": media_type,
            },
            ensure_ascii=False,
        ),
        "response_json": "{}",
        "verdict": "error",
        "expected_status": case.expected_status,
        "actual_status": None,
        "assertions_json": "[]",
        "duration_ms": duration_ms,
        "failure_reason": str(reason)[:500],
    }


async def _execute_case(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: ApiEndpoint,
    case: ApiEndpointCase,
    extra_headers: dict | None = None,
) -> dict:
    """单条用例执行：构造请求 → httpx 调用 → 状态码断言（D-021）+ 响应体字段断言（D-027）→ 结构化快照。

    断言评估先于 4KB 截断快照——对完整响应体求值；发送方式按接口快照的
    request_body_media_type 选择（D-024）：json → json=，urlencoded → data=，
    multipart → 普通字段 data= + 二进制字段占位文件 files=。
    extra_headers 为登录态派生头（如 Authorization），用例自身的 headers 优先。
    """
    try:
        request = json.loads(case.request_json) if case.request_json else {}
    except (TypeError, ValueError):
        request = {}
    try:
        assertion_specs = json.loads(case.assertions_json) if case.assertions_json else []
    except (TypeError, ValueError):
        assertion_specs = []
    if not isinstance(assertion_specs, list):
        assertion_specs = []

    # 形状非法 → 单条判 error；这些字段原先在逐条 try 之外被使用，非对象会让整轮中断
    shape_error = _invalid_request_shape(request)
    if shape_error:
        return _error_result(endpoint, case, shape_error)
    request = request if isinstance(request, dict) else {}

    path = endpoint.path
    for name, value in (request.get("path") or {}).items():
        # 编码后再代入：含 / ? # 或中文的 path 参数不编码会改变请求语义
        path = path.replace(f"{{{name}}}", quote(str(value), safe=""))
    url = f"{base_url.rstrip('/')}{path}"
    query = request.get("query") or {}
    headers = {**(extra_headers or {}), **(request.get("headers") or {})}
    body = request.get("body")
    media_type = endpoint.request_body_media_type or "application/json"

    started = time.perf_counter()
    try:
        kwargs: dict = {"params": query or None, "headers": headers or None}
        if body is not None and media_type == "application/x-www-form-urlencoded":
            kwargs["data"] = body
        elif body is not None and media_type == "multipart/form-data":
            data, files = _split_form_fields(endpoint.request_body_json, body)
            kwargs["data"] = data or None
            kwargs["files"] = files or None
        else:
            kwargs["json"] = body if body is not None else None
        response = await client.request(endpoint.method.upper(), url, **kwargs)
        duration_ms = int((time.perf_counter() - started) * 1000)
        assertion_results = api_assertions.evaluate_assertions(assertion_specs, response.text)
        status_ok = response.status_code == case.expected_status
        verdict = "passed" if status_ok and all(r["passed"] for r in assertion_results) else "failed"
        # 响应头可能含 Set-Cookie、响应体可能回显 token：落库前脱敏（执行详情对规格可读者开放，D-023）
        response_snapshot = {
            "status": response.status_code,
            "headers": _redact_headers(dict(list(response.headers.items())[:_MAX_HEADER_SNAPSHOT])),
            "body": (
                _redact_json_body(response.text)
                if len(response.text) <= _MAX_BODY_SNAPSHOT
                else response.text[:_MAX_BODY_SNAPSHOT]
            ),
        }
        return {
            "endpoint_id": endpoint.id,
            "case_name": case.name,
            "request_json": json.dumps(
                {
                    "method": endpoint.method.upper(),
                    "url": url,
                    "query": query,
                    "headers": _redact_headers(headers),
                    "body": _redact_body(body),
                    "media_type": media_type,
                },
                ensure_ascii=False,
            ),
            "response_json": json.dumps(response_snapshot, ensure_ascii=False),
            "verdict": verdict,
            "expected_status": case.expected_status,
            "actual_status": response.status_code,
            "assertions_json": json.dumps(_redact_assertion_actuals(assertion_results), ensure_ascii=False),
            "duration_ms": duration_ms,
            "failure_reason": _compose_failure_reason(status_ok, case.expected_status, response.status_code, assertion_results),
        }
    except Exception as e:  # 超时/连接拒绝等 → error（区别于断言失败）
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.warning("用例 %s 执行异常: %s", case.name, e)
        return _error_result(
            endpoint,
            case,
            str(e),
            url=url,
            query=query,
            headers=headers,
            body=body,
            media_type=media_type,
            duration_ms=duration_ms,
        )


def _compose_failure_reason(status_ok: bool, expected_status: int, actual_status: int, assertion_results: list[dict]) -> str | None:
    """失败原因组合：状态码部分在前，断言失败摘要在后；全部通过返回 None"""
    parts = []
    if not status_ok:
        parts.append(f"预期 {expected_status}，实际 {actual_status}")
    assertion_summary = api_assertions.summarize_failures(assertion_results)
    if assertion_summary:
        parts.append(f"断言失败：{assertion_summary}")
    return "；".join(parts) if parts else None


def _save_result(run_id: str, result: dict) -> None:
    db = create_session()
    try:
        db.add(TestRunResult(run_id=run_id, **result))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("保存用例结果失败: %s", e, exc_info=True)  # 单条落库失败不终止执行
    finally:
        db.close()


def redact_stored_request_headers() -> int:
    """存量结果行清洗（幂等，启动时调用）：把快照中的敏感头/凭据字段改写为 "***"。

    写侧脱敏上线前的历史数据仍含明文凭据（Authorization/Cookie、登录类用例的
    password、响应回显的 token），仅靠读侧兜底不能消除静态存储暴露。
    LIKE 预筛覆盖全部敏感头与体字段名，保证清洗完成后每次启动只剩一次空扫。
    返回改写的行数。
    """
    db = create_session()
    rewritten = 0
    try:
        patterns = list(_SENSITIVE_HEADERS) + sorted(_SENSITIVE_BODY_KEYS)
        conditions = [
            column.like(f"%{pattern}%")
            for column in (TestRunResult.request_json, TestRunResult.response_json, TestRunResult.assertions_json)
            for pattern in patterns
        ]
        candidates = db.query(TestRunResult).filter(or_(*conditions)).all()
        for row in candidates:
            changed = False

            try:
                request = json.loads(row.request_json)
            except (TypeError, ValueError):
                request = None
            if isinstance(request, dict):
                request_changed = False
                headers = request.get("headers")
                if isinstance(headers, dict):
                    redacted = _redact_headers(headers)
                    if redacted != headers:
                        request["headers"] = redacted
                        request_changed = True
                body = request.get("body")
                if isinstance(body, dict):
                    redacted = _redact_body(body)
                    if redacted != body:
                        request["body"] = redacted
                        request_changed = True
                if request_changed:
                    row.request_json = json.dumps(request, ensure_ascii=False)
                    changed = True

            try:
                response = json.loads(row.response_json)
            except (TypeError, ValueError):
                response = None
            if isinstance(response, dict):
                headers = response.get("headers")
                if isinstance(headers, dict):
                    redacted = _redact_headers(headers)
                    if redacted != headers:
                        response["headers"] = redacted
                        row.response_json = json.dumps(response, ensure_ascii=False)
                        changed = True

            try:
                assertions = json.loads(row.assertions_json) if row.assertions_json else []
            except (TypeError, ValueError):
                assertions = []
            if isinstance(assertions, list):
                redacted = _redact_assertion_actuals(assertions)
                if redacted != assertions:
                    row.assertions_json = json.dumps(redacted, ensure_ascii=False)
                    changed = True

            if changed:
                rewritten += 1
        if rewritten:
            db.commit()
            logger.info("已脱敏 %d 条历史执行结果中的凭据信息", rewritten)
        return rewritten
    except Exception as e:
        db.rollback()
        logger.error("清洗历史执行结果失败: %s", e, exc_info=True)
        return rewritten
    finally:
        db.close()


def reset_stale_running() -> int:
    """启动时把残留的 running 执行置为 failed（幂等）。

    执行不跨进程存活（无 checkpoint），重启后这些 run 永远等不到收尾；而占用语义
    按 status == "running" 判断 → 该文档被永久锁死（本人转订阅，但进程内没有对应
    运行、他人 409）。返回重置条数。
    """
    db = create_session()
    try:
        rows = db.query(TestRun).filter(TestRun.status == "running").all()
        if not rows:
            return 0
        for row in rows:
            row.status = "failed"
            row.error = "执行因服务重启中断，请重试"
            row.finished_at = func.now()
        db.commit()
        logger.warning("已重置 %d 条因服务重启中断的执行记录", len(rows))
        return len(rows)
    except Exception as e:
        db.rollback()
        logger.error("重置中断的执行记录失败: %s", e, exc_info=True)
        return 0
    finally:
        db.close()


def _finish_run(run_id: str, status: str, passed: int, failed: int, errored: int, total: int, error: str | None) -> None:
    db = create_session()
    try:
        row = db.query(TestRun).filter(TestRun.id == run_id).first()
        if row is None:
            return
        row.status = status
        row.passed = passed
        row.failed = failed
        row.errored = errored
        row.total = total
        # 与 created_at 同源（DB 时间）：应用与 DB 时区不同时不会出现 finished_at 早于 created_at
        row.finished_at = func.now()
        row.error = error
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("收尾落库失败: %s", e, exc_info=True)
    finally:
        db.close()


async def execute_run(run_id: str, base_url: str, endpoint_ids: list[str] | None, hub) -> None:
    """后台顺序执行（不绑定 HTTP 请求）：结果逐条落库 + case_done 事件；收尾汇总落库 + completed 事件。

    hub 为 RunHub 实例；发布按 run_id key（执行协程不持有 handle）。
    """
    run = await asyncio.to_thread(_get_run, run_id)
    if run is None:
        return
    spec_id = run.spec_id
    passed = failed = errored = 0
    failed_error = None
    cases: list = []
    auth_config = None
    client: httpx.AsyncClient | None = None
    try:
        # 加载、client 创建、首帧事件全部纳入 try/finally：任何一步失败都必须收尾，
        # 否则 run 永远停在 running（占用语义按 status 判断 → 该文档被锁死）且 client 不关闭
        cases = await asyncio.to_thread(_load_run_cases, spec_id, endpoint_ids)
        auth_config = await asyncio.to_thread(api_spec_service.get_auth_config, spec_id)
        endpoints_summary = []
        for endpoint_row, _case in cases:
            label = f"{endpoint_row.method.upper()} {endpoint_row.path}"
            if label not in endpoints_summary:
                endpoints_summary.append(label)
        client = _create_run_client()
        hub.publish_key(run_id, {
            "event": "run_started",
            "run_id": run_id,
            "total": len(cases),
            "endpoints": endpoints_summary,
            "auth": {
                "method": str(auth_config.get("method") or "post").upper(),
                "path": auth_config["path"],
            } if auth_config else None,
        })

        # 登录态前置请求（D-025）：失败则本轮直接 failed，不用例请求
        auth_headers = {}
        if auth_config:
            auth_method = str(auth_config.get("method") or "post")
            auth_path = str(auth_config.get("path") or "")
            declared_media_type, auth_body_json = await asyncio.to_thread(
                _load_auth_endpoint, spec_id, auth_method, auth_path
            )
            auth_headers, auth_error = await _prepare_auth_session(
                client,
                run_id,
                base_url,
                auth_config,
                hub,
                declared_media_type=declared_media_type,
                auth_body_json=auth_body_json,
            )
            if auth_error:
                failed_error = auth_error
        if failed_error is None:
            for endpoint, case in cases:
                result = await _execute_case(client, base_url, endpoint, case, extra_headers=auth_headers)
                if result["verdict"] == "passed":
                    passed += 1
                elif result["verdict"] == "failed":
                    failed += 1
                else:
                    errored += 1
                await asyncio.to_thread(_save_result, run_id, result)
                hub.publish_key(run_id, {
                    "event": "case_done",
                    "endpoint": f"{endpoint.method.upper()} {endpoint.path}",
                    "case_name": case.name,
                    "verdict": result["verdict"],
                    "expected_status": result["expected_status"],
                    "actual_status": result["actual_status"],
                    "duration_ms": result["duration_ms"],
                    "failure_reason": result["failure_reason"],
                    "response": json.loads(result["response_json"]) if result["response_json"] else {},
                    "assertions": json.loads(result["assertions_json"]) if result["assertions_json"] else [],
                    "passed": passed,
                    "failed": failed,
                    "errored": errored,
                })
    except Exception as e:
        logger.error("测试执行 %s 异常: %s", run_id, e, exc_info=True)
        failed_error = f"测试执行异常：{e}"
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        status = "failed" if failed_error else "completed"
        try:
            await asyncio.to_thread(
                _finish_run, run_id, status, passed, failed, errored, len(cases), failed_error
            )
        except Exception as e:
            logger.error("执行收尾失败: %s", e, exc_info=True)
        # 先发终态事件再 finish（finish 的哨兵会唤醒订阅者收尾）
        completion = {
            "event": "failed" if failed_error else "completed",
            "run_id": run_id,
            "total": len(cases),
            "passed": passed,
            "failed": failed,
            "errored": errored,
        }
        if failed_error:
            completion["error"] = failed_error
        hub.publish_key(run_id, completion)
        hub.finish_key(run_id)


def _load_auth_endpoint(spec_id: str, method: str, path: str) -> tuple[str | None, str | None]:
    """登录目标端点声明的 (请求体媒体类型, 请求体 schema JSON)；未匹配返回 (None, None)。

    2.0 formData 的媒体类型由解析层归一为 multipart/form-data，这里取回 schema 是为了
    multipart 登录接口能按 binary 字段发占位文件——只发 data= 会被严格实现判 422。
    """
    db = create_session()
    try:
        row = (
            db.query(ApiEndpoint.request_body_media_type, ApiEndpoint.request_body_json)
            .filter(
                ApiEndpoint.spec_id == spec_id,
                ApiEndpoint.method == method.lower(),
                ApiEndpoint.path == path,
            )
            .first()
        )
        if row is None:
            return None, None
        return (row[0] or None), (row[1] or None)
    finally:
        db.close()


async def _prepare_auth_session(
    client: httpx.AsyncClient,
    run_id: str,
    base_url: str,
    auth_config: dict,
    hub,
    declared_media_type: str | None = None,
    auth_body_json: str | None = None,
) -> tuple[dict, str | None]:
    """登录态前置请求（D-025）：发送登录请求，Cookie 由 httpx 自动收集进本轮 client 的
    cookie jar，后续用例自动携带；配置 token_field 时从 2xx 响应 JSON 提取 token
    生成 Authorization: Bearer 头。登录请求不计入用例结果。

    请求体媒体类型以规格中匹配端点的声明优先（表单端点收 JSON 必 422），
    配置的 body_type 仅在端点未声明/不存在时生效；声明 multipart 时按 schema 的
    binary 字段发占位文件（否则 httpx 会退化成 urlencoded）。

    返回 (额外请求头, 失败原因)；失败时本轮执行以该原因 failed。
    """
    method = str(auth_config.get("method") or "post").upper()
    path = str(auth_config.get("path") or "/")
    body = auth_config.get("body") or {}
    body_type = auth_config.get("body_type") or "json"
    if declared_media_type == "multipart/form-data":
        body_type = "multipart"
    elif declared_media_type in ("application/x-www-form-urlencoded", "application/json"):
        body_type = "json" if declared_media_type == "application/json" else "form"
    token_field = auth_config.get("token_field") or None
    url = f"{base_url.rstrip('/')}{path}"
    if body_type == "json":
        kwargs: dict = {"json": body}
    elif body_type == "multipart":
        data, files = _split_form_fields(auth_body_json, body if isinstance(body, dict) else {})
        kwargs = {"data": data or None, "files": files or None}
    else:
        kwargs = {"data": body}

    try:
        response = await client.request(method, url, **kwargs)
    except Exception as e:
        message = f"登录态获取失败：{e}"[:300]
        hub.publish_key(run_id, {"event": "auth_done", "ok": False, "status": None, "message": message})
        return {}, message

    extra_headers: dict = {}
    if token_field and 200 <= response.status_code < 300:
        try:
            payload = response.json()
            token = payload.get(token_field) if isinstance(payload, dict) else None
            if token:
                extra_headers["Authorization"] = f"Bearer {token}"
        except ValueError:
            pass

    # 会话是否真的建立：拿到 token 或 cookie jar 里有 Cookie。只看 3xx 会把
    # "302 但没下发任何凭据"误判成功，失败随后被归因到每一条用例
    session_established = bool(extra_headers.get("Authorization")) or len(client.cookies.jar) > 0
    ok = 200 <= response.status_code < 300 or (300 <= response.status_code < 400 and session_established)
    if ok:
        message = f"登录态获取成功（HTTP {response.status_code}）"
    elif 300 <= response.status_code < 400:
        message = f"登录态获取失败：HTTP {response.status_code}（未取得 Cookie/Token）"
    else:
        message = f"登录态获取失败：HTTP {response.status_code}"
    if response.status_code == 422:
        message += "（请检查登录请求体类型与字段是否匹配登录接口）"
    hub.publish_key(run_id, {"event": "auth_done", "ok": ok, "status": response.status_code, "message": message})
    logger.info("执行 %s 登录态请求 %s %s → HTTP %s（body_type=%s）", run_id, method, path, response.status_code, body_type)
    return extra_headers, (None if ok else message)
