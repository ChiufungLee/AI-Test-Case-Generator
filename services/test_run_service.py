"""测试执行服务：进程内 httpx 顺序执行接口用例（D-012），结果与事件经 RunHub 发布。

占用语义：同规格已有 running 运行时，本人转订阅（返回 None）、他人占用转 409（ConflictError）；
执行不绑定 HTTP 请求，客户端断开只影响 SSE 订阅。断言 v1 仅状态码比对（D-021）。
"""

import asyncio
import json
import logging
import time

import httpx
from sqlalchemy import or_

from config import get_api_test_timeout
from models.api_test_models import ApiEndpoint, ApiEndpointCase, ApiSpec, TestRun, TestRunResult
from models.database import create_session

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """资源不存在或不可见（端点转 404）"""


class ConflictError(Exception):
    """同规格已有他人执行中的运行（端点转 409）"""


_MAX_BODY_SNAPSHOT = 4096
_MAX_HEADER_SNAPSHOT = 20

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """进程内共享 AsyncClient（连接池复用）。单事件循环内同步段无抢占，无需加锁；
    测试经 monkeypatch 替换 _client 为 MockTransport 客户端。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(get_api_test_timeout()))
    return _client


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
            raise NotFoundError("API 规格不存在")

        running = (
            db.query(TestRun)
            .filter(TestRun.spec_id == spec_id, TestRun.status == "running")
            .first()
        )
        if running is not None:
            if running.created_by == user_id:
                return None
            raise ConflictError("该规格已有其他用户执行中的运行")

        if not str(base_url).startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")

        total = _count_enabled_cases(db, spec_id, endpoint_ids)
        run = TestRun(
            spec_id=spec_id,
            base_url=base_url,
            status="running",
            total=total,
            created_by=user_id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        logger.info("用户 %s 创建测试执行 %s（%s 条用例）", user_id, run.id, total)
        return run
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


def _count_enabled_cases(db, spec_id: str, endpoint_ids: list[str] | None) -> int:
    query = (
        db.query(ApiEndpointCase)
        .join(ApiEndpoint, ApiEndpointCase.endpoint_id == ApiEndpoint.id)
        .filter(ApiEndpoint.spec_id == spec_id, ApiEndpointCase.enabled == True)  # noqa: E712
    )
    if endpoint_ids:
        query = query.filter(ApiEndpoint.id.in_(endpoint_ids))
    return query.count()


def get_run_view(run_id: str, user_id: int) -> dict | None:
    """执行详情（含逐条结果）；owner-only（结果含请求/响应细节，不跨用户暴露）"""
    db = create_session()
    try:
        run = db.query(TestRun).filter(TestRun.id == run_id, TestRun.created_by == user_id).first()
        if run is None:
            return None
        results = (
            db.query(TestRunResult)
            .filter(TestRunResult.run_id == run.id)
            .order_by(TestRunResult.created_at.asc(), TestRunResult.id.asc())
            .all()
        )
        return {**run_payload(run), "results": [result_payload(r) for r in results]}
    finally:
        db.close()


def list_runs(spec_id: str, user_id: int) -> list[dict]:
    """规格的执行历史（本人创建的）"""
    db = create_session()
    try:
        rows = (
            db.query(TestRun)
            .filter(TestRun.spec_id == spec_id, TestRun.created_by == user_id)
            .order_by(TestRun.created_at.desc())
            .limit(50)
            .all()
        )
        return [run_payload(row) for row in rows]
    finally:
        db.close()


def run_payload(run: TestRun) -> dict:
    return {
        "id": run.id,
        "spec_id": run.spec_id,
        "base_url": run.base_url,
        "status": run.status,
        "total": run.total,
        "passed": run.passed,
        "failed": run.failed,
        "errored": run.errored,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


def result_payload(result: TestRunResult) -> dict:
    try:
        request = json.loads(result.request_json)
    except (TypeError, ValueError):
        request = {}
    try:
        response = json.loads(result.response_json)
    except (TypeError, ValueError):
        response = {}
    return {
        "id": result.id,
        "case_name": result.case_name,
        "request": request,
        "response": response,
        "verdict": result.verdict,
        "expected_status": result.expected_status,
        "actual_status": result.actual_status,
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


async def _execute_case(client: httpx.AsyncClient, base_url: str, endpoint: ApiEndpoint, case: ApiEndpointCase) -> dict:
    """单条用例执行：构造请求 → httpx 调用 → 状态码断言（v1，D-021）→ 结构化快照"""
    try:
        request = json.loads(case.request_json) if case.request_json else {}
    except (TypeError, ValueError):
        request = {}
    path = endpoint.path
    for name, value in (request.get("path") or {}).items():
        path = path.replace(f"{{{name}}}", str(value))
    url = f"{base_url.rstrip('/')}{path}"
    query = request.get("query") or {}
    headers = request.get("headers") or {}
    body = request.get("body")

    started = time.perf_counter()
    try:
        response = await client.request(
            endpoint.method.upper(),
            url,
            params=query or None,
            headers=headers or None,
            json=body if body is not None else None,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        verdict = "passed" if response.status_code == case.expected_status else "failed"
        failure_reason = None if verdict == "passed" else f"预期 {case.expected_status}，实际 {response.status_code}"
        response_snapshot = {
            "status": response.status_code,
            "headers": dict(list(response.headers.items())[:_MAX_HEADER_SNAPSHOT]),
            "body": response.text[:_MAX_BODY_SNAPSHOT],
        }
        return {
            "endpoint_id": endpoint.id,
            "case_name": case.name,
            "request_json": json.dumps(
                {"method": endpoint.method.upper(), "url": url, "query": query, "headers": headers, "body": body},
                ensure_ascii=False,
            ),
            "response_json": json.dumps(response_snapshot, ensure_ascii=False),
            "verdict": verdict,
            "expected_status": case.expected_status,
            "actual_status": response.status_code,
            "duration_ms": duration_ms,
            "failure_reason": failure_reason,
        }
    except Exception as e:  # 超时/连接拒绝等 → error（区别于断言失败）
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.warning("用例 %s 执行异常: %s", case.name, e)
        return {
            "endpoint_id": endpoint.id,
            "case_name": case.name,
            "request_json": json.dumps(
                {"method": endpoint.method.upper(), "url": url, "query": query, "headers": headers, "body": body},
                ensure_ascii=False,
            ),
            "response_json": "{}",
            "verdict": "error",
            "expected_status": case.expected_status,
            "actual_status": None,
            "duration_ms": duration_ms,
            "failure_reason": str(e)[:500],
        }


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


def _finish_run(run_id: str, status: str, passed: int, failed: int, errored: int, total: int, error: str | None) -> None:
    db = create_session()
    try:
        from datetime import datetime

        row = db.query(TestRun).filter(TestRun.id == run_id).first()
        if row is None:
            return
        row.status = status
        row.passed = passed
        row.failed = failed
        row.errored = errored
        row.total = total
        row.finished_at = datetime.now()
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
    run = _get_run(run_id)
    if run is None:
        return
    spec_id = run.spec_id
    cases = _load_run_cases(spec_id, endpoint_ids)
    client = get_http_client()
    passed = failed = errored = 0
    failed_error = None

    hub.publish_key(run_id, {"event": "run_started", "run_id": run_id, "total": len(cases)})
    try:
        for endpoint, case in cases:
            result = await _execute_case(client, base_url, endpoint, case)
            if result["verdict"] == "passed":
                passed += 1
            elif result["verdict"] == "failed":
                failed += 1
            else:
                errored += 1
            await asyncio.to_thread(_save_result, run_id, result)
            hub.publish_key(run_id, {
                "event": "case_done",
                "case_name": case.name,
                "verdict": result["verdict"],
                "expected_status": result["expected_status"],
                "actual_status": result["actual_status"],
                "duration_ms": result["duration_ms"],
                "passed": passed,
                "failed": failed,
                "errored": errored,
            })
    except Exception as e:
        logger.error("测试执行 %s 异常: %s", run_id, e, exc_info=True)
        failed_error = f"测试执行异常：{e}"
    finally:
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
