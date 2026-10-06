"""接口用例服务：规则引擎生成、AI 业务建议（两段式，D-017 同构）、用例整表替换"""

import asyncio
import json
import logging

from sqlalchemy import or_

from models.api_test_models import ApiEndpoint, ApiEndpointCase, ApiSpec
from models.database import create_session
from prompts.prompts import get_workflow_prompt_messages, get_workflow_temperature
from schemas.api_test_schemas import ApiCaseProposalSet
from services import api_case_engine
from services.api_spec_service import NotFoundError, endpoint_payload
# 经模块属性调用 nodes._invoke_structured（而非 from-import 早期绑定），
# tests 的 monkeypatch 桩才能保持生效（D-018）
from workflows import nodes as workflow_nodes

logger = logging.getLogger(__name__)


class AISuggestError(Exception):
    """AI 建议链路失败（LLM 调用或输出校验未通过；端点转 502，细节只写日志）"""


# ---------- 可见范围辅助 ----------


def _spec_scope(db, set_id: str, user_id: int, writable: bool):
    """规格可见范围：writable 仅 owner（写操作）；否则 owner 或共享（读操作）"""
    query = db.query(ApiSpec).filter(ApiSpec.id == set_id)
    if writable:
        query = query.filter(ApiSpec.owner_user_id == user_id)
    else:
        query = query.filter(or_(ApiSpec.owner_user_id == user_id, ApiSpec.visibility == "shared"))
    return query.first()


def _endpoint_in_scope(db, set_id: str, endpoint_id: str, user_id: int, writable: bool):
    """校验规格可见性（writable 时仅 owner）与端点归属；返回 (spec, endpoint)，越界抛 NotFoundError"""
    spec = _spec_scope(db, set_id, user_id, writable)
    if spec is None:
        raise NotFoundError("接口文档不存在")
    endpoint = (
        db.query(ApiEndpoint)
        .filter(ApiEndpoint.spec_id == set_id, ApiEndpoint.id == endpoint_id)
        .first()
    )
    if endpoint is None:
        raise NotFoundError("接口不存在")
    return spec, endpoint


def case_payload(case: ApiEndpointCase) -> dict:
    try:
        request = json.loads(case.request_json)
    except (TypeError, ValueError):
        request = {}
    try:
        assertions = json.loads(case.assertions_json) if case.assertions_json else []
    except (TypeError, ValueError):
        assertions = []
    return {
        "id": case.id,
        "name": case.name,
        "request": request,
        "expected_status": case.expected_status,
        "assertions": assertions if isinstance(assertions, list) else [],
        "source_type": case.source_type,
        "enabled": bool(case.enabled),
    }


# ---------- 查询与生成 ----------


def list_cases(set_id: str, endpoint_id: str, user_id: int) -> list[dict]:
    """接口用例清单（共享读者可用）"""
    db = create_session()
    try:
        _, endpoint = _endpoint_in_scope(db, set_id, endpoint_id, user_id, writable=False)
        rows = (
            db.query(ApiEndpointCase)
            .filter(ApiEndpointCase.endpoint_id == endpoint.id)
            .order_by(ApiEndpointCase.created_at.asc(), ApiEndpointCase.name.asc())
            .all()
        )
        return [case_payload(row) for row in rows]
    finally:
        db.close()


def generate_cases(set_id: str, endpoint_id: str, user_id: int) -> list[dict]:
    """规则引擎幂等重生成：仅替换 rule_engine 旧用例，manual/ai 保留；与现有用例同名时 422"""
    db = create_session()
    try:
        _, endpoint = _endpoint_in_scope(db, set_id, endpoint_id, user_id, writable=True)
        proposal_rows = api_case_engine.generate_case_proposals(endpoint_payload(endpoint))

        existing_names = {
            row.name
            for row in db.query(ApiEndpointCase)
            .filter(
                ApiEndpointCase.endpoint_id == endpoint.id,
                ApiEndpointCase.source_type != "rule_engine",
            )
            .all()
        }
        conflicts = [row["name"] for row in proposal_rows if row["name"] in existing_names]
        if conflicts:
            raise ValueError(f"生成的用例名称与现有用例冲突：{conflicts[0]}")

        db.query(ApiEndpointCase).filter(
            ApiEndpointCase.endpoint_id == endpoint.id,
            ApiEndpointCase.source_type == "rule_engine",
        ).delete(synchronize_session=False)
        db.add_all([
            ApiEndpointCase(
                endpoint_id=endpoint.id,
                name=row["name"],
                request_json=json.dumps(row["request"], ensure_ascii=False),
                expected_status=row["expected_status"],
                assertions_json=json.dumps(row.get("assertions") or [], ensure_ascii=False),
                source_type="rule_engine",
            )
            for row in proposal_rows
        ])
        db.commit()
        logger.info("接口 %s 规则引擎生成 %s 条用例", endpoint.id, len(proposal_rows))
    except (NotFoundError, ValueError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("规则引擎生成用例失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()
    return list_cases(set_id, endpoint_id, user_id)


# ---------- 整表替换（AI 确认落库与手工编辑共用） ----------


def save_cases(set_id: str, endpoint_id: str, user_id: int, cases: list[dict]) -> list[dict]:
    """整表替换接口用例；批内名称重复 422（唯一约束 (endpoint_id, name) 的前置校验）"""
    names = [case.get("name") for case in cases]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"用例名称重复：{duplicates[0]}")

    db = create_session()
    try:
        _, endpoint = _endpoint_in_scope(db, set_id, endpoint_id, user_id, writable=True)
        db.query(ApiEndpointCase).filter(
            ApiEndpointCase.endpoint_id == endpoint.id
        ).delete(synchronize_session=False)
        db.add_all([
            ApiEndpointCase(
                endpoint_id=endpoint.id,
                name=case["name"],
                request_json=json.dumps(case.get("request") or {}, ensure_ascii=False),
                expected_status=int(case.get("expected_status") or 200),
                assertions_json=json.dumps(case.get("assertions") or [], ensure_ascii=False),
                source_type=case.get("source_type") or "manual",
                enabled=bool(case.get("enabled", True)),
            )
            for case in cases
        ])
        db.commit()
        logger.info("接口 %s 保存 %s 条用例（整表替换）", endpoint.id, len(cases))
    except (NotFoundError, ValueError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("保存接口用例失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()
    return list_cases(set_id, endpoint_id, user_id)


# ---------- AI 业务建议（两段式第一步：提案不落库） ----------


def _load_ai_suggest_baseline(set_id: str, endpoint_id: str, user_id: int) -> tuple[dict, list[dict]]:
    db = create_session()
    try:
        _, endpoint = _endpoint_in_scope(db, set_id, endpoint_id, user_id, writable=True)
        rows = (
            db.query(ApiEndpointCase)
            .filter(ApiEndpointCase.endpoint_id == endpoint.id)
            .order_by(ApiEndpointCase.created_at.asc())
            .all()
        )
        return endpoint_payload(endpoint), [case_payload(row) for row in rows]
    finally:
        db.close()


async def ai_suggest_cases(set_id: str, endpoint_id: str, user_id: int, instruction: str) -> dict:
    """AI 业务异常用例建议：LLM 结构化输出提案（不落库），前端确认后经 save_cases 落库。

    结构化调用复用 workflows.nodes 解析链，经模块属性调用保持测试桩兼容（D-018）；
    「调用 + 校验」自建 2 次尝试循环（校验失败=提案重名/空/结构非法，也触发重试）。
    """
    endpoint_dict, existing_cases = await asyncio.to_thread(
        _load_ai_suggest_baseline, set_id, endpoint_id, user_id
    )

    messages = get_workflow_prompt_messages(
        "openapi_business_cases_workflow",
        instruction=instruction,
        endpoint_json=json.dumps(endpoint_dict, ensure_ascii=False),
        existing_cases_json=json.dumps(existing_cases, ensure_ascii=False),
    )
    temperature = get_workflow_temperature("openapi_business_cases_workflow")

    proposals = None
    truncated = False
    last_error = None
    for attempt in (1, 2):
        try:
            result, truncated = await workflow_nodes._invoke_structured(
                messages, ApiCaseProposalSet, temperature
            )
            proposals = _validated_proposals(result)
            break
        except Exception as e:
            last_error = e
            logger.warning("AI 建议第 %s/2 次尝试失败: %s", attempt, e)
    if proposals is None:
        raise AISuggestError(f"AI 建议失败: {last_error}") from last_error

    logger.info(
        "接口 %s AI 建议预览：%s 条提案，截断=%s", endpoint_id, len(proposals), truncated
    )
    return {
        "endpoint_id": endpoint_id,
        "truncated": truncated,
        "proposals": proposals,
    }


def _validated_proposals(result: ApiCaseProposalSet) -> list[dict]:
    """提案校验：批内名称去重 + 非空 + request 结构合法；非法抛 ValueError 触发重试"""
    proposals = [p.model_dump() for p in result.proposals]
    if not proposals:
        raise ValueError("AI 未给出任何提案")
    names = [p["name"] for p in proposals]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"AI 提案名称重复：{duplicates[0]}")
    for proposal in proposals:
        if not isinstance(proposal.get("request"), dict):
            raise ValueError("AI 提案的 request 必须是对象")
    return proposals
