import asyncio
import json
import logging
import re
from collections import Counter

from langchain_core.messages import BaseMessage
from langgraph.types import interrupt
from pydantic import BaseModel

from config import get_workflow_llm_max_tokens
from models.database import create_session
from prompts.prompts import get_workflow_prompt_messages, get_workflow_temperature
from schemas.workflow_schemas import (
    CoverageReport,
    DuplicatePair,
    RequirementAnalysis,
    TestCaseSet,
    TestPointSet,
)
from services import workflow_service
from utils.llm_handle import get_llm_model
from utils.retriever import get_rag_retriever_by_kb
from workflows.state import TestWorkflowState
from workflows.test_design import (
    DUPLICATE_SIMILARITY_THRESHOLD,
    build_test_design_plan,
    case_quality_issues,
    case_similarity,
    dynamic_test_point_limit,
    find_duplicate_test_points,
    normalize_case_ids,
    requirement_ids_from_analysis,
    validate_test_points,
)

logger = logging.getLogger(__name__)


# ---------- 确定性节点 ----------


async def load_requirement(state: TestWorkflowState) -> dict:
    """读取工作流的原始需求与知识库配置；作为每轮新运行的起点，顺带清空上次失败的 error"""
    workflow_id = state["workflow_id"]
    row = await asyncio.to_thread(workflow_service.get_workflow, workflow_id)
    if row is None:
        return {"error": f"工作流不存在: {workflow_id}"}

    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="analyzing",
        current_step="load_requirement",
        error=None,
    )
    return {
        "requirement_text": row.requirement_text,
        "knowledge_base_id": row.knowledge_base_id,
    }


async def retrieve_knowledge(state: TestWorkflowState) -> dict:
    """按需求文本做一次知识库检索（v1 为确定性检索，不做 agent 工具循环）。

    检索结果落库为 retrieved_context Artifact，供用户事后回看中间产物。
    """
    kb_id = state.get("knowledge_base_id")
    if not kb_id:
        return {"retrieved_documents": []}

    db = create_session()
    try:
        retriever = await get_rag_retriever_by_kb(kb_id, db, state["user_id"])
    finally:
        db.close()

    if not retriever:
        logger.info("工作流 %s 未获取到知识库 %s 的检索器，跳过检索", state["workflow_id"], kb_id)
        return {"retrieved_documents": []}

    try:
        docs = await retriever.get_relevant_documents(state.get("requirement_text") or "")
    except Exception as e:
        logger.error("工作流知识库检索失败: %s", e, exc_info=True)
        return {"retrieved_documents": []}

    documents = [_format_document(doc) for doc in docs]
    await asyncio.to_thread(
        workflow_service.save_artifact,
        state["workflow_id"],
        "retrieved_context",
        {"knowledge_base_id": kb_id, "documents": documents},
    )
    logger.info("工作流 %s 检索到 %s 个相关文档", state["workflow_id"], len(docs))
    return {"retrieved_documents": documents}


async def coverage_check(state: TestWorkflowState) -> dict:
    """覆盖校验与质检节点：纯确定性代码（对照测试点/覆盖项清单核算 + 相似度查重），产出报告并完结工作流"""
    workflow_id = state["workflow_id"]
    report = build_coverage_report(
        state.get("requirement_analysis") or {},
        state.get("test_cases") or [],
        test_point_set=state.get("test_point_set") or {},
        test_design_plan=state.get("test_design_plan") or {},
        failed_test_points=state.get("failed_test_points") or [],
    )
    await asyncio.to_thread(workflow_service.save_artifact, workflow_id, "coverage_report", report)
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="completed",
        current_step="coverage_check",
        error=None,
    )
    return {"coverage_report": report}


async def persist_failure(state: TestWorkflowState) -> dict:
    """失败落库节点：把 error 持久化到工作流行"""
    error = state.get("error") or "未知错误"
    logger.error("工作流 %s 失败: %s", state.get("workflow_id"), error)
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        state["workflow_id"],
        status="failed",
        error=error,
    )
    return {}


def build_coverage_report(
    analysis: dict,
    test_cases: list[dict],
    test_point_set: dict | None = None,
    test_design_plan: dict | None = None,
    failed_test_points: list[str] | None = None,
) -> dict:
    """确定性的覆盖校验与质检（不调用 LLM，D-036 阶段 5）。

    口径（不再是"用例自我声明"）：
    - 测试点覆盖：测试点是否被至少一条用例的 test_point_refs 引用
    - 覆盖项覆盖：阶段 3 产出的覆盖项是否被至少一条用例的 covered_items 声明
    - 需求覆盖：用例所关联测试点关联的需求点，并集用例自行声明的有效需求引用
    - 质检：字段完整性、非法引用、疑似重复（用例与测试点）、维度分布、失败批次
    """
    requirement_ids = requirement_ids_from_analysis(analysis)
    test_points = [
        item for item in ((test_point_set or {}).get("test_points") or []) if isinstance(item, dict)
    ]
    test_point_ids = {item.get("id") for item in test_points if item.get("id")}
    point_requirements = {
        item.get("id"): set(item.get("requirement_refs") or [])
        for item in test_points
        if item.get("id")
    }
    all_coverage_items = {
        item.get("id") for item in _iter_coverage_items(test_design_plan) if item.get("id")
    }

    covered_points: set[str] = set()
    declared_items: set[str] = set()
    covered_requirements: set[str] = set()
    invalid_point_refs: set[str] = set()
    invalid_requirement_refs: set[str] = set()

    for case in test_cases:
        for ref in case.get("test_point_refs") or []:
            if ref in test_point_ids:
                covered_points.add(ref)
                covered_requirements |= point_requirements.get(ref, set())
            else:
                invalid_point_refs.add(ref)
        for ref in case.get("covered_items") or []:
            declared_items.add(ref)
        for ref in case.get("requirement_refs") or []:
            if ref in requirement_ids:
                covered_requirements.add(ref)
            else:
                invalid_requirement_refs.add(ref)

    duplicates = []
    for i in range(len(test_cases)):
        for j in range(i + 1, len(test_cases)):
            score = case_similarity(test_cases[i], test_cases[j])
            if score >= DUPLICATE_SIMILARITY_THRESHOLD:
                duplicates.append(
                    DuplicatePair(
                        case_a=test_cases[i].get("id", ""),
                        case_b=test_cases[j].get("id", ""),
                        similarity=round(score, 2),
                    ).model_dump()
                )

    uncovered_points = sorted(test_point_ids - covered_points)
    quality_issues = case_quality_issues(test_cases)
    if uncovered_points:
        quality_issues.append("以下测试点没有任何用例：" + "、".join(uncovered_points))
    if failed_test_points:
        quality_issues.append(
            "以下测试点的用例生成失败，建议重新生成：" + "、".join(sorted(set(failed_test_points)))
        )

    report = CoverageReport(
        total_cases=len(test_cases),
        total_test_points=len(test_point_ids),
        covered_test_points=sorted(covered_points),
        uncovered_test_points=uncovered_points,
        invalid_test_point_refs=sorted(invalid_point_refs),
        uncovered_coverage_items=sorted(all_coverage_items - declared_items),
        invalid_covered_items=sorted(declared_items - all_coverage_items),
        covered_requirements=sorted(covered_requirements),
        uncovered_requirements=sorted(requirement_ids - covered_requirements),
        invalid_refs=sorted(invalid_requirement_refs),
        dimension_summary=dict(
            sorted(Counter(item.get("dimension") or "function" for item in test_points).items())
        ),
        priority_summary=dict(sorted(Counter(case.get("priority") for case in test_cases).items())),
        duplicates=duplicates,
        duplicate_test_points=find_duplicate_test_points(test_points),
        quality_issues=quality_issues,
        failed_test_points=sorted(set(failed_test_points or [])),
        note=(
            "覆盖依据测试点与覆盖项清单核算：测试点覆盖＝被至少一条用例的 test_point_refs 引用；"
            "覆盖项覆盖＝被至少一条用例的 covered_items 声明；"
            "需求覆盖＝用例所关联测试点关联的需求点，并集用例自行声明的有效需求引用。"
            "业务规则与风险的覆盖未自动统计，请结合用例覆盖说明（rationale）人工复核。"
        ),
    )
    return report.model_dump()


def _iter_coverage_items(test_design_plan: dict | None):
    """遍历设计策略中的覆盖项（设计策略可空，兼容未产出该产物的历史任务）"""
    for item in (test_design_plan or {}).get("items") or []:
        for coverage_item in item.get("coverage_items") or []:
            if isinstance(coverage_item, dict):
                yield coverage_item


def _format_document(doc) -> dict:
    filename = doc.metadata.get("filename", "未知文件")
    page = doc.metadata.get("page")
    source = f"《{filename}》" + (f" 第{page}页" if page else "")
    return {"source": source, "content": doc.page_content}


def _format_documents(documents: list[dict]) -> str:
    return "\n\n---\n\n".join(f"[来源: {d['source']}]\n{d['content']}" for d in documents)


# ---------- Agent 节点（结构化 LLM 调用） ----------


async def requirement_analysis_agent(state: TestWorkflowState) -> dict:
    """需求分析 agent：结构化产出 RequirementAnalysis 并落库为 Artifact v1"""
    workflow_id = state["workflow_id"]
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="analyzing",
        current_step="requirement_analysis_agent",
    )

    context = _format_documents(state.get("retrieved_documents") or [])
    messages = get_workflow_prompt_messages(
        "requirement_analysis_workflow",
        context=context,
        requirement_text=state.get("requirement_text") or "",
    )

    try:
        analysis, analysis_truncated = await _call_structured_with_retry(
            messages,
            RequirementAnalysis,
            get_workflow_temperature("requirement_analysis_workflow"),
        )
    except Exception as e:
        logger.error("需求分析结构化输出失败: %s", e, exc_info=True)
        return {"error": f"需求分析失败：{e}"}

    if analysis_truncated:
        # 分析产物会经人工确认节点展示，截断缺失由人工把关，不静默也不阻断
        logger.warning("工作流 %s 需求分析输出被 max_tokens 截断，结果可能不完整", workflow_id)

    analysis_dict = analysis.model_dump()
    artifact_id = await asyncio.to_thread(
        workflow_service.save_artifact, workflow_id, "requirement_analysis", analysis_dict
    )
    return {"requirement_analysis": analysis_dict, "analysis_artifact_id": artifact_id}


async def test_point_design_agent(state: TestWorkflowState) -> dict:
    """测试点设计 agent（D-036 阶段 2）：产出"测什么"——测试点清单并落库为 Artifact。

    LLM 只负责识别测试点与覆盖维度；引用合法性、重复、超限由确定性规则
    （workflows.test_design.validate_test_points）后处理，丢弃项与问题随产物落库。
    """
    workflow_id = state["workflow_id"]
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="analyzing",
        current_step="test_point_design_agent",
    )

    analysis = state.get("requirement_analysis") or {}
    requirement_ids = requirement_ids_from_analysis(analysis)
    context = _format_documents(state.get("retrieved_documents") or [])
    messages = get_workflow_prompt_messages(
        "test_point_design_workflow",
        context=context,
        analysis_json=json.dumps(analysis, ensure_ascii=False),
        test_point_limit=dynamic_test_point_limit(len(requirement_ids)),
    )

    try:
        result, truncated = await _call_structured_with_retry(
            messages,
            TestPointSet,
            get_workflow_temperature("test_point_design_workflow"),
        )
    except Exception as e:
        logger.error("测试点设计结构化输出失败: %s", e, exc_info=True)
        return {"error": f"测试点设计失败：{e}"}

    if truncated:
        logger.warning("工作流 %s 测试点输出被 max_tokens 截断，结果可能不完整", workflow_id)

    test_points, dropped, issues = validate_test_points(
        [item.model_dump() for item in result.test_points], requirement_ids
    )
    content = {
        "test_points": test_points,
        "coverage_target": result.coverage_target,
        "notes": list(result.notes),
        "dropped_test_points": dropped,
        "issues": issues,
        "truncated": truncated,
    }
    if not test_points:
        return {"error": "测试点设计未产出有效测试点（全部未关联到有效需求点）"}

    artifact_id = await asyncio.to_thread(
        workflow_service.save_artifact, workflow_id, "test_point_set", content
    )
    logger.info("工作流 %s 产出 %s 个测试点", workflow_id, len(test_points))
    return {"test_point_set": content, "test_point_artifact_id": artifact_id}


async def human_review(state: TestWorkflowState) -> dict:
    """人工确认节点：interrupt 暂停图等待确认，resume 携带确认载荷。

    resume 载荷为 {"action": "approve", "analysis": dict | None}（恒为非空 dict，
    避免 langgraph 对 resume=None / 空 dict 的边界行为）；analysis 为 None 表示
    原样确认，为 dict 时校验通过后覆盖 state，内容有变化则追加新版 Artifact
    （parent 指向原版本）。
    """
    workflow_id = state["workflow_id"]
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="waiting_review",
        current_step="human_review",
    )

    resumed = interrupt(
        {
            "type": "requirement_analysis_review",
            "analysis": state.get("requirement_analysis") or {},
            "message": "请确认或修改需求分析结果后继续",
        }
    )

    if not isinstance(resumed, dict):
        return {}
    analysis = resumed.get("analysis")
    if not isinstance(analysis, dict) or not analysis:
        return {}

    try:
        validated = RequirementAnalysis.model_validate(analysis)
    except Exception as e:
        return {"error": f"人工修改后的需求分析未通过校验：{e}"}

    analysis_dict = validated.model_dump()
    if analysis_dict != (state.get("requirement_analysis") or {}):
        artifact_id = await asyncio.to_thread(
            workflow_service.save_artifact,
            workflow_id,
            "requirement_analysis",
            analysis_dict,
            parent_artifact_id=state.get("analysis_artifact_id"),
        )
        return {"requirement_analysis": analysis_dict, "analysis_artifact_id": artifact_id}
    return {}


async def test_design_plan_node(state: TestWorkflowState) -> dict:
    """测试设计策略节点（D-036 阶段 3）：纯确定性——技术选型、覆盖项骨架与用例预算。

    D-020 的"确定性规则引擎为主"在工作流域的落地：只有取值填充与业务语义留给 LLM。
    """
    workflow_id = state["workflow_id"]
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="analyzing",
        current_step="test_design_plan",
    )

    test_points = (state.get("test_point_set") or {}).get("test_points") or []
    plan = build_test_design_plan(test_points)
    artifact_id = await asyncio.to_thread(
        workflow_service.save_artifact, workflow_id, "test_design_plan", plan
    )
    return {"test_design_plan": plan, "design_artifact_id": artifact_id}


async def human_review_design(state: TestWorkflowState) -> dict:
    """测试点与设计策略的人工确认节点（D-036 ⑤）：interrupt 等待确认，resume 载荷
    为 {"action": "approve", "test_point_set": dict | None}。

    测试点被修改时：落新版 test_point_set Artifact，并由确定性代码**重算**设计策略
    追加新版本（设计策略不接受人工直接编辑——手改会与规则脱节）。原样确认则直接继续。
    """
    workflow_id = state["workflow_id"]
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="waiting_review",
        current_step="human_review_design",
    )

    current = state.get("test_point_set") or {}
    resumed = interrupt(
        {
            "type": "test_point_review",
            "test_point_set": current,
            "test_design_plan": state.get("test_design_plan") or {},
            "message": "请确认或修改测试点后继续；设计策略会依据测试点自动重算",
        }
    )

    if not isinstance(resumed, dict):
        return {}
    edited = resumed.get("test_point_set")
    if not isinstance(edited, dict) or not edited:
        return {}

    try:
        validated = TestPointSet.model_validate(edited)
    except Exception as e:
        return {"error": f"人工修改后的测试点未通过校验：{e}"}

    points = [item.model_dump() for item in validated.test_points]
    requirement_ids = requirement_ids_from_analysis(state.get("requirement_analysis") or {})
    invalid = sorted({ref for item in points for ref in item["requirement_refs"] if ref not in requirement_ids})
    if invalid:
        return {"error": "人工修改后的测试点引用了不存在的需求点：" + "、".join(invalid)}
    duplicate_ids = sorted({pid for pid, count in Counter(item["id"] for item in points).items() if count > 1})
    if duplicate_ids:
        return {"error": "人工修改后的测试点编号重复：" + "、".join(duplicate_ids)}

    if points == (current.get("test_points") or []) and validated.coverage_target == current.get("coverage_target"):
        return {}

    content = {
        "test_points": points,
        "coverage_target": validated.coverage_target,
        "notes": list(validated.notes),
        "dropped_test_points": [],
        "issues": [],
        "truncated": False,
    }
    artifact_id = await asyncio.to_thread(
        workflow_service.save_artifact,
        workflow_id,
        "test_point_set",
        content,
        state.get("test_point_artifact_id"),
    )
    plan = build_test_design_plan(points)
    design_id = await asyncio.to_thread(
        workflow_service.save_artifact,
        workflow_id,
        "test_design_plan",
        plan,
        state.get("design_artifact_id"),
    )
    return {
        "test_point_set": content,
        "test_point_artifact_id": artifact_id,
        "test_design_plan": plan,
        "design_artifact_id": design_id,
    }


async def test_case_generation_agent(state: TestWorkflowState) -> dict:
    """用例生成 agent（D-036 阶段 4）：按测试点分批设计用例。

    每批只喂一个测试点 + 该点的设计策略（技术 + 覆盖项 + 用例预算），单批输出小、
    可重试、失败可隔离（失败的测试点记入 failed_test_points 并继续其余批次）；
    用例编号由服务端统一分配（normalize_case_ids），不依赖 LLM 保证跨批唯一。
    """
    workflow_id = state["workflow_id"]
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="generating",
        current_step="test_case_generation_agent",
    )

    analysis = state.get("requirement_analysis") or {}
    analysis_json = json.dumps(analysis, ensure_ascii=False)
    context = _format_documents(state.get("retrieved_documents") or [])
    test_points = (state.get("test_point_set") or {}).get("test_points") or []
    plan_items = {
        item.get("test_point_id"): item
        for item in (state.get("test_design_plan") or {}).get("items") or []
    }
    if not test_points:
        return {"error": "缺少测试点，无法生成用例"}

    cases: list[dict] = []
    failed_test_points: list[str] = []
    truncated_any = False

    for test_point in test_points:
        test_point_id = test_point.get("id")
        plan_item = plan_items.get(test_point_id) or {
            "test_point_id": test_point_id,
            "techniques": [],
            "coverage_items": [],
            "case_budget": 1,
        }
        case_budget = max(1, int(plan_item.get("case_budget") or 1))
        messages = get_workflow_prompt_messages(
            "testcase_generation_workflow",
            context=context,
            analysis_json=analysis_json,
            test_point_json=json.dumps(test_point, ensure_ascii=False),
            design_plan_item_json=json.dumps(plan_item, ensure_ascii=False),
            test_point_id=test_point_id,
            case_budget=case_budget,
        )
        try:
            result, truncated = await _call_structured_with_retry(
                messages,
                TestCaseSet,
                get_workflow_temperature("testcase_generation_workflow"),
            )
        except Exception as e:
            logger.error("测试点 %s 用例生成失败: %s", test_point_id, e, exc_info=True)
            failed_test_points.append(test_point_id)
            continue

        truncated_any = truncated_any or truncated
        for case in result.test_cases[:case_budget]:
            item = case.model_dump()
            # 测试点归属以本批为准（LLM 可能漏填或填错），需求引用为空时回落到测试点的需求
            item["test_point_refs"] = sorted({*(item.get("test_point_refs") or []), test_point_id})
            if not item.get("requirement_refs"):
                item["requirement_refs"] = list(test_point.get("requirement_refs") or [])
            cases.append(item)

    if not cases:
        return {"error": "测试用例生成失败：所有测试点均未产出用例"}

    cases = normalize_case_ids(cases)
    if truncated_any:
        logger.warning("工作流 %s 部分批次输出被 max_tokens 截断，用例可能不完整", workflow_id)

    content = {
        "test_cases": cases,
        "truncated": truncated_any,
        "failed_test_points": failed_test_points,
    }
    await asyncio.to_thread(workflow_service.save_artifact, workflow_id, "test_case_set", content)
    return {
        "test_cases": cases,
        "cases_truncated": truncated_any,
        "failed_test_points": failed_test_points,
    }


# ---------- 结构化 LLM 调用封装（测试在此处打桩） ----------


async def _call_structured_with_retry(messages: list[BaseMessage], schema, temperature: float, attempts: int = 2):
    """结构化调用 + 重试；全部失败时抛出最后一次异常。

    返回 (结果, truncated)：truncated=True 表示输出被 max_tokens 截断、
    由 _salvage_truncated_json 抢救出已完成的部分，调用方应透出该标记而不是静默展示残缺结果。
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _invoke_structured(messages, schema, temperature)
        except Exception as e:
            last_error = e
            logger.warning("结构化输出第 %s/%s 次尝试失败: %s", attempt, attempts, e)
    raise last_error  # type: ignore[misc]


async def _invoke_structured(messages: list[BaseMessage], schema, temperature: float) -> tuple[BaseModel, bool]:
    """单次结构化调用：纯文本 JSON 解析优先（对思考模型最稳、日志干净），失败再回退 with_structured_output。

    结构化输出的 max_tokens 独立配置（需容纳 reasoning + 完整 JSON，默认 16384）；
    解析失败时先尝试抢救被 max_tokens 截断的 JSON，保留已完成的部分并返回 truncated=True。
    """
    max_tokens = get_workflow_llm_max_tokens()
    model = get_llm_model().bind(max_tokens=max_tokens)
    response = await model.ainvoke(messages, temperature=temperature)
    raw = _extract_text(response.content)

    plain_error = None
    try:
        return schema.model_validate_json(_extract_json_object(raw)), False
    except Exception as e:
        plain_error = e

    salvaged = _salvage_truncated_json(raw)
    if salvaged:
        try:
            logger.warning("JSON 输出疑似被截断，抢救出 %s 字符后重新校验", len(salvaged))
            return schema.model_validate_json(salvaged), True
        except Exception:
            pass

    logger.warning("纯文本 JSON 解析失败，回退 with_structured_output 重试: %s", plain_error)
    try:
        structured = model.with_structured_output(schema)
        parsed = await structured.ainvoke(messages, temperature=temperature)
    except Exception as e:
        # 思考模型在复杂 prompt 上会拒绝 tool_choice（400），属预期兜底路径
        logger.info("with_structured_output 兜底也未成功: %s", e)
    else:
        if parsed is None:
            # 模型回了正文却没走工具调用时，langchain 的 PydanticToolsParser(first_tool_only=True)
            # 会返回 None 而不是抛错（langchain_core/output_parsers/openai_tools.py）。
            # 必须在这里转成异常：否则 _call_structured_with_retry 把它当成成功返回，
            # 调用方在 result.xxx 上抛 AttributeError，整轮运行以「'NoneType' object has
            # no attribute ...」告终（线上事故，D-044）。
            logger.warning("with_structured_output 返回空结果（模型未按 schema 调用工具）")
            raise ValueError(
                "模型未返回结构化结果（既不是可解析的 JSON 正文，也没有工具调用）"
            )
        return parsed, False
    raise plain_error


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict):
            parts.append(block.get("text", ""))
        else:
            parts.append(str(block))
    return "".join(parts)


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM 返回内容中未找到 JSON 对象")
    return stripped[start : end + 1]


def _salvage_truncated_json(text: str) -> str | None:
    """抢救被 max_tokens 截断的 JSON：裁剪到最后一个完整闭合的子对象，补齐剩余括号。

    返回 None 表示内容完整（闭合深度归零）或无法定位任何完整子对象，抢救不适用。
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw).strip()
    start = raw.find("{")
    if start == -1:
        return None
    body = raw[start:]

    depth = 0
    in_string = False
    escaped = False
    last_safe_end = -1
    for i, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return None  # JSON 本身完整，截断抢救不适用
            if depth >= 1:
                last_safe_end = i + 1
    # 末尾 in_string=True 表示截断发生在字符串中间，last_safe_end 位于该字符串之前，仍可抢救
    if last_safe_end == -1:
        return None

    candidate = body[:last_safe_end]
    closers: list[str] = []
    in_str = False
    esc = False
    for ch in candidate:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            closers.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if closers:
                closers.pop()
    if not closers:
        return None
    return candidate + "".join(reversed(closers))
