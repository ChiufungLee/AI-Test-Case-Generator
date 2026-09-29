import asyncio
import json
import logging
import re
from collections import Counter

from langchain_core.messages import BaseMessage
from langgraph.types import interrupt
from rapidfuzz import fuzz

from config import get_workflow_llm_max_tokens
from models.database import create_session
from prompts.prompts import get_workflow_prompt_messages, get_workflow_temperature
from schemas.workflow_schemas import (
    CoverageReport,
    DuplicatePair,
    RequirementAnalysis,
    TestCaseSet,
)
from services import workflow_service
from utils.llm_handle import get_llm_model
from utils.retriever import get_rag_retriever_by_kb
from workflows.state import TestWorkflowState

logger = logging.getLogger(__name__)

# 标题相似度达到该阈值时判定为疑似重复用例
DUPLICATE_SIMILARITY_THRESHOLD = 0.85


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
    """覆盖检查节点：纯确定性代码（集合运算 + 相似度查重），产出覆盖报告并完结工作流"""
    workflow_id = state["workflow_id"]
    report = build_coverage_report(
        state.get("requirement_analysis") or {},
        state.get("test_cases") or [],
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


def build_coverage_report(analysis: dict, test_cases: list[dict]) -> dict:
    """确定性的覆盖检查：需求点覆盖、非法引用、优先级分布、疑似重复（不调用 LLM）"""
    requirement_ids = {
        item.get("id")
        for item in (analysis.get("functional_requirements") or [])
        if item.get("id")
    }

    covered: set[str] = set()
    invalid: set[str] = set()
    for case in test_cases:
        for ref in case.get("requirement_refs") or []:
            if ref in requirement_ids:
                covered.add(ref)
            else:
                invalid.add(ref)

    duplicates = []
    titles = [(case.get("id", ""), case.get("title", "")) for case in test_cases]
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            score = _title_similarity(titles[i][1], titles[j][1])
            if score >= DUPLICATE_SIMILARITY_THRESHOLD:
                duplicates.append(
                    DuplicatePair(
                        case_a=titles[i][0], case_b=titles[j][0], similarity=round(score, 2)
                    ).model_dump()
                )

    report = CoverageReport(
        total_cases=len(test_cases),
        covered_requirements=sorted(covered),
        uncovered_requirements=sorted(requirement_ids - covered),
        invalid_refs=sorted(invalid),
        priority_summary=dict(sorted(Counter(case.get("priority") for case in test_cases).items())),
        duplicates=duplicates,
    )
    return report.model_dump()


def _title_similarity(a: str, b: str) -> float:
    """标题相似度：中文无空格分词，先按字符切分为 token 再做词序无关比较"""
    return fuzz.token_sort_ratio(" ".join(a), " ".join(b)) / 100.0


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
        analysis = await _call_structured_with_retry(
            messages,
            RequirementAnalysis,
            get_workflow_temperature("requirement_analysis_workflow"),
        )
    except Exception as e:
        logger.error("需求分析结构化输出失败: %s", e, exc_info=True)
        return {"error": f"需求分析失败：{e}"}

    analysis_dict = analysis.model_dump()
    artifact_id = await asyncio.to_thread(
        workflow_service.save_artifact, workflow_id, "requirement_analysis", analysis_dict
    )
    return {"requirement_analysis": analysis_dict, "analysis_artifact_id": artifact_id}


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


async def test_case_generation_agent(state: TestWorkflowState) -> dict:
    """用例生成 agent：消费上游需求分析（含人工修订）+ 检索证据，产出 TestCaseSet"""
    workflow_id = state["workflow_id"]
    await asyncio.to_thread(
        workflow_service.update_workflow_status,
        workflow_id,
        status="generating",
        current_step="test_case_generation_agent",
    )

    context = _format_documents(state.get("retrieved_documents") or [])
    analysis_json = json.dumps(state.get("requirement_analysis") or {}, ensure_ascii=False)
    messages = get_workflow_prompt_messages(
        "testcase_generation_workflow",
        context=context,
        analysis_json=analysis_json,
    )

    try:
        result = await _call_structured_with_retry(
            messages,
            TestCaseSet,
            get_workflow_temperature("testcase_generation_workflow"),
        )
    except Exception as e:
        logger.error("测试用例结构化输出失败: %s", e, exc_info=True)
        return {"error": f"测试用例生成失败：{e}"}

    cases = [case.model_dump() for case in result.test_cases]
    await asyncio.to_thread(
        workflow_service.save_artifact, workflow_id, "test_case_set", {"test_cases": cases}
    )
    return {"test_cases": cases}


# ---------- 结构化 LLM 调用封装（测试在此处打桩） ----------


async def _call_structured_with_retry(messages: list[BaseMessage], schema, temperature: float, attempts: int = 2):
    """结构化调用 + 重试；全部失败时抛出最后一次异常"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _invoke_structured(messages, schema, temperature)
        except Exception as e:
            last_error = e
            logger.warning("结构化输出第 %s/%s 次尝试失败: %s", attempt, attempts, e)
    raise last_error  # type: ignore[misc]


async def _invoke_structured(messages: list[BaseMessage], schema, temperature: float):
    """单次结构化调用：纯文本 JSON 解析优先（对思考模型最稳、日志干净），失败再回退 with_structured_output。

    结构化输出的 max_tokens 独立配置（需容纳 reasoning + 完整 JSON，默认 16384）；
    解析失败时先尝试抢救被 max_tokens 截断的 JSON，保留已完成的部分。
    """
    max_tokens = get_workflow_llm_max_tokens()
    model = get_llm_model().bind(max_tokens=max_tokens)
    response = await model.ainvoke(messages, temperature=temperature)
    raw = _extract_text(response.content)

    plain_error = None
    try:
        return schema.model_validate_json(_extract_json_object(raw))
    except Exception as e:
        plain_error = e

    salvaged = _salvage_truncated_json(raw)
    if salvaged:
        try:
            logger.warning("JSON 输出疑似被截断，抢救出 %s 字符后重新校验", len(salvaged))
            return schema.model_validate_json(salvaged)
        except Exception:
            pass

    logger.warning("纯文本 JSON 解析失败，回退 with_structured_output 重试: %s", plain_error)
    try:
        structured = model.with_structured_output(schema)
        return await structured.ainvoke(messages, temperature=temperature)
    except Exception as e:
        # 思考模型在复杂 prompt 上会拒绝 tool_choice（400），属预期兜底路径
        logger.info("with_structured_output 兜底也未成功: %s", e)
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
