import asyncio
import json
import logging
from pathlib import Path

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from sqlalchemy.orm import Session

from api.endpoints.run_hub import RunHub
from models.database import get_db

from schemas.workflow_schemas import (
    ApproveRequest,
    RegenerateRequest,
    RequirementAnalysis,
    WorkflowCreate,
)
from services import knowledge_service, workflow_service
from services.auth_service import require_user
from utils.data_handle import testcases_to_csv
from workflows.graph import get_compiled_graph

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
logger = logging.getLogger(__name__)


def _artifact_response(artifact) -> dict:
    try:
        content = json.loads(artifact.content)
    except (TypeError, ValueError):
        content = {"raw": artifact.content}
    return {
        "id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "version": artifact.version,
        "parent_artifact_id": artifact.parent_artifact_id,
        "content": content,
        "created_at": artifact.created_at,
    }


def _workflow_summary(workflow) -> dict:
    return {
        "id": workflow.id,
        "name": workflow.name,
        "status": workflow.status,
        "current_step": workflow.current_step,
        "error": workflow.error,
        "knowledge_base_id": workflow.knowledge_base_id,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }


def _event_artifact(node_name: str, update) -> dict | None:
    """从节点输出中提取产物载荷随 SSE 下发，让前端在节点完成瞬间即可渲染结果。

    节点完成事件若不带载荷，前端只能展示详情接口返回的旧产物快照，
    流中新产生的检索结果/分析/用例会显示为空。
    """
    if not isinstance(update, dict):
        return None
    if node_name == "retrieve_knowledge" and update.get("retrieved_documents") is not None:
        return {
            "artifact_type": "retrieved_context",
            "content": {"documents": update["retrieved_documents"]},
        }
    if node_name == "requirement_analysis_agent" and update.get("requirement_analysis") is not None:
        return {
            "artifact_type": "requirement_analysis",
            "content": update["requirement_analysis"],
        }
    if node_name == "test_case_generation_agent" and update.get("test_cases") is not None:
        return {
            "artifact_type": "test_case_set",
            "content": {
                "test_cases": update["test_cases"],
                "truncated": bool(update.get("cases_truncated")),
            },
        }
    if node_name == "coverage_check" and update.get("coverage_report") is not None:
        return {
            "artifact_type": "coverage_report",
            "content": update["coverage_report"],
        }
    return None


# 进程内运行枢纽：工作流后台执行的发布/订阅基建（与 API 测试执行共用 RunHub，D-021）
_run_hub = RunHub()


def _start_run(workflow_id: str, run_input, config_extra: dict | None = None):
    """注册并启动后台执行任务；已有运行时直接返回该运行（调用方变为订阅者）"""
    return _run_hub.register(
        workflow_id,
        lambda: asyncio.create_task(_run_workflow_graph(workflow_id, run_input, config_extra)),
    )


async def _run_workflow_graph(workflow_id: str, run_input, config_extra: dict | None = None) -> None:
    """后台执行工作流图（不绑定任何 HTTP 请求）：客户端断开只影响订阅，不影响执行。

    run_input 语义：
    - dict  → 全新/重跑（thread 已结束时从头再跑一轮）
    - None  → 从 checkpoint 断点续跑（进程重启恢复或 waiting_review 下再次拉起）
    - Command(resume=...) → 人工确认后恢复 interrupt
    config_extra 可注入 checkpoint_id 实现时间旅行（重新生成用例）。
    """
    run = _run_hub.get(workflow_id)
    graph = await get_compiled_graph()
    configurable = {"thread_id": workflow_id}
    if config_extra:
        configurable.update(config_extra)
    config = {"configurable": configurable}

    interrupted_payload = None
    failed_error = None
    try:
        async for chunk in graph.astream(run_input, config=config, stream_mode="updates"):
            if "__interrupt__" in chunk:
                interrupts = chunk["__interrupt__"]
                interrupted_payload = interrupts[0].value if interrupts else {}
                analysis = (interrupted_payload or {}).get("analysis", {})
                _run_hub.publish(run, {"event": "waiting_review", "analysis": analysis})
                continue

            for node_name, update in chunk.items():
                if isinstance(update, dict) and update.get("error"):
                    failed_error = update["error"]
                event_data = {"event": "node_done", "node": node_name}
                artifact = _event_artifact(node_name, update)
                if artifact:
                    event_data["artifact"] = artifact
                _run_hub.publish(run, event_data)

        if failed_error:
            _run_hub.publish(run, {"event": "failed", "error": failed_error})
        elif interrupted_payload is None:
            _run_hub.publish(run, {"event": "completed"})
    except Exception as e:
        logger.error("工作流 %s 后台运行异常: %s", workflow_id, e, exc_info=True)
        failed_error = f"工作流运行异常：{e}"
        await asyncio.to_thread(
            workflow_service.update_workflow_status, workflow_id, status="failed", error=failed_error
        )
        _run_hub.publish(run, {"event": "failed", "error": failed_error})
    finally:
        _run_hub.finish(workflow_id, run)


@router.get("/workflows", response_class=HTMLResponse)
def workflow_page(request: Request):
    username = request.session.get("username")
    if username is None:
        return templates.TemplateResponse(request, "login.html", {"error": "用户会话已失效，请重新登录"})
    return templates.TemplateResponse(
        request, "workflow.html", {"username": username, "user_id": request.session.get("user_id")}
    )


@router.post("/api/workflows")
def create_workflow_endpoint(
    data: WorkflowCreate,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    name = (data.name or "").strip() or "新任务"
    requirement_text = (data.requirement_text or "").strip()
    if not requirement_text:
        return JSONResponse(status_code=400, content={"error": "需求内容不能为空"})

    knowledge_base_id = data.knowledge_base_id or None
    if knowledge_base_id:
        kb = knowledge_service.get_knowledge_base_by_id(
            kb_id=knowledge_base_id, db=db, user_id=user_id, allow_shared_read=True
        )
        if not kb:
            return JSONResponse(status_code=404, content={"error": "知识库不存在"})
        knowledge_base_id = kb.id

    workflow = workflow_service.create_workflow(
        user_id=user_id,
        name=name,
        requirement_text=requirement_text,
        knowledge_base_id=knowledge_base_id,
    )
    return _workflow_summary(workflow)


@router.get("/api/workflows")
def list_workflows_endpoint(user_id: int = Depends(require_user)):

    workflows = workflow_service.list_workflows(user_id)
    return {"workflows": [_workflow_summary(w) for w in workflows]}


@router.get("/api/workflows/{workflow_id}")
def get_workflow_endpoint(workflow_id: str, user_id: int = Depends(require_user)):

    workflow = workflow_service.get_owned_workflow(user_id, workflow_id)
    if not workflow:
        return JSONResponse(status_code=404, content={"error": "工作流不存在"})

    result = _workflow_summary(workflow)
    result["requirement_text"] = workflow.requirement_text
    result["artifacts"] = [_artifact_response(a) for a in workflow_service.get_artifacts(workflow_id)]
    return result


@router.post("/api/workflows/{workflow_id}/start")
async def start_workflow_endpoint(workflow_id: str, user_id: int = Depends(require_user)):
    async with _run_hub.lock:
        workflow = await asyncio.to_thread(workflow_service.get_owned_workflow, user_id, workflow_id)
        if not workflow:
            return JSONResponse(status_code=404, content={"error": "任务不存在"})

        # created/failed：从头跑一轮，用状态机乐观锁互斥（并发 start 只有一个抢占成功）；
        # waiting_review/analyzing/generating：从 checkpoint 续跑（进程重启恢复场景），
        # 已有后台运行时 _start_run 直接返回该运行，本次请求退化为订阅者
        if workflow.status in ("created", "failed"):
            claimed = await asyncio.to_thread(
                workflow_service.try_claim_workflow,
                workflow.id,
                ("created", "failed"),
                status="analyzing",
                current_step="load_requirement",
                error=None,
            )
            if not claimed:
                return JSONResponse(status_code=409, content={"error": "任务已在运行中，请刷新页面查看"})
            run_input = {"workflow_id": workflow.id, "user_id": user_id, "error": None}
        else:
            run_input = None

        run = _start_run(workflow.id, run_input)

    queue = _run_hub.subscribe(run)
    return StreamingResponse(
        _run_hub.forward_events(run, queue),
        media_type="text/event-stream",
    )


@router.post("/api/workflows/{workflow_id}/approve")
async def approve_workflow_endpoint(
    workflow_id: str,
    data: ApproveRequest,
    user_id: int = Depends(require_user),
):
    async with _run_hub.lock:
        workflow = await asyncio.to_thread(workflow_service.get_owned_workflow, user_id, workflow_id)
        if not workflow:
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        if workflow.status != "waiting_review":
            return JSONResponse(
                status_code=409,
                content={"error": f"任务当前状态为 {workflow.status}，无法确认继续"},
            )

        analysis = data.analysis
        if analysis is not None:
            try:
                RequirementAnalysis.model_validate(analysis)
            except Exception as e:
                return JSONResponse(
                    status_code=422, content={"error": f"修改后的需求分析未通过校验：{e}"}
                )

        # resume 载荷恒为非空 dict（langgraph 对 resume=None/空 dict 有边界问题）；
        # analysis=None 表示原样确认
        resume_payload = {"action": "approve", "analysis": analysis}
        run = _start_run(workflow.id, Command(resume=resume_payload))

    queue = _run_hub.subscribe(run)
    return StreamingResponse(
        _run_hub.forward_events(run, queue),
        media_type="text/event-stream",
    )


@router.post("/api/workflows/{workflow_id}/regenerate")
async def regenerate_workflow_endpoint(
    workflow_id: str,
    data: RegenerateRequest | None = None,
    user_id: int = Depends(require_user),
):
    """对用例生成结果不满意时，从"人工确认之后"的 checkpoint 时间旅行重跑生成节点。

    可携带人工修订后的需求分析（analysis）：修订版落库为新版本 Artifact 并通过
    update_state 写入该 checkpoint 的状态分支，生成节点将消费修订后的分析。
    未携带时沿用当前分析原样重跑。需求分析之前的部分不重新执行。
    """
    async with _run_hub.lock:
        workflow = await asyncio.to_thread(workflow_service.get_owned_workflow, user_id, workflow_id)
        if not workflow:
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        if workflow.status not in ("completed", "failed"):
            return JSONResponse(
                status_code=409,
                content={"error": f"任务当前状态为 {workflow.status}，无法重新生成用例"},
            )

        analysis_dict = None
        if data is not None and data.analysis is not None:
            try:
                validated = RequirementAnalysis.model_validate(data.analysis)
            except Exception as e:
                return JSONResponse(
                    status_code=422,
                    content={"error": f"修改后的需求分析未通过校验：{e}"},
                )
            analysis_dict = validated.model_dump()

        graph = await get_compiled_graph()
        target_config = None
        async for snapshot in graph.aget_state_history({"configurable": {"thread_id": workflow_id}}):
            if snapshot.next and snapshot.next[0] == "test_case_generation_agent":
                target_config = snapshot.config
                break
        if target_config is None:
            return JSONResponse(
                status_code=409,
                content={"error": "未找到可用的用例生成检查点，无法重新生成；请重新创建任务"},
            )

        config_extra = {"checkpoint_id": target_config["configurable"]["checkpoint_id"]}

        if analysis_dict is not None:
            latest_analysis = await asyncio.to_thread(
                workflow_service.get_latest_artifact, workflow_id, "requirement_analysis"
            )
            latest_content = (
                json.loads(latest_analysis.content)
                if latest_analysis and latest_analysis.content
                else None
            )
            if latest_content != analysis_dict:
                # 内容有变化才落新版本并写回状态分支，避免"原样重跑"产生冗余版本
                await asyncio.to_thread(
                    workflow_service.save_artifact,
                    workflow_id,
                    "requirement_analysis",
                    analysis_dict,
                    parent_artifact_id=latest_analysis.id if latest_analysis else None,
                )
                updated_config = await graph.aupdate_state(
                    target_config, {"requirement_analysis": analysis_dict}
                )
                config_extra = {"checkpoint_id": updated_config["configurable"]["checkpoint_id"]}

        await asyncio.to_thread(
            workflow_service.update_workflow_status,
            workflow_id,
            status="generating",
            current_step="test_case_generation_agent",
            error=None,
        )
        run = _start_run(workflow_id, None, config_extra=config_extra)

    queue = _run_hub.subscribe(run)
    return StreamingResponse(
        _run_hub.forward_events(run, queue),
        media_type="text/event-stream",
    )


@router.get("/api/workflows/{workflow_id}/export")
def export_workflow_testcases(workflow_id: str, user_id: int = Depends(require_user)):

    workflow = workflow_service.get_owned_workflow(user_id, workflow_id)
    if not workflow:
        return JSONResponse(status_code=404, content={"error": "任务不存在"})

    artifact = workflow_service.get_latest_artifact(workflow_id, "test_case_set")
    if not artifact:
        return JSONResponse(status_code=404, content={"error": "未找到测试用例产物"})

    try:
        content = json.loads(artifact.content)
    except (TypeError, ValueError):
        return JSONResponse(status_code=500, content={"error": "测试用例产物损坏"})

    csv_data = testcases_to_csv(content.get("test_cases") or [])
    headers = {
        "Content-Disposition": f"attachment; filename=workflow_{workflow_id}_testcases.csv",
        "Content-Type": "text/csv; charset=utf-8",
    }
    # utf-8-sig 带 BOM，保证中文在 Excel 中不乱码
    return Response(content=csv_data.encode("utf-8-sig"), headers=headers)
