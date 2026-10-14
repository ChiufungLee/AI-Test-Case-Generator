import asyncio
import json
import logging
from pathlib import Path

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from sqlalchemy.orm import Session

from api.endpoints.run_hub import RunHub
from models.database import get_db

from schemas.workflow_schemas import (
    ApproveRequest,
    RegenerateRequest,
    RequirementAnalysis,
    TestPointSet,
    WorkflowCreate,
    WorkflowRename,
)
from services import knowledge_service, workflow_service
from services.auth_service import require_user
from utils.data_handle import testcases_to_csv
from workflows.graph import delete_workflow_checkpoints, get_compiled_graph
from workflows.test_design import build_test_design_plan, requirement_ids_from_analysis

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
    # 列表卡片要展示需求摘要，但列表不该携带完整需求原文（详情接口单独返回 requirement_text）
    excerpt = " ".join((workflow.requirement_text or "").split())
    return {
        "id": workflow.id,
        "name": workflow.name,
        "status": workflow.status,
        "current_step": workflow.current_step,
        "error": workflow.error,
        "knowledge_base_id": workflow.knowledge_base_id,
        "requirement_file_name": workflow.requirement_file_name,
        "requirement_excerpt": excerpt[:120],
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
    if node_name == "test_point_design_agent" and update.get("test_point_set") is not None:
        return {
            "artifact_type": "test_point_set",
            "content": update["test_point_set"],
        }
    if node_name == "test_design_plan" and update.get("test_design_plan") is not None:
        return {
            "artifact_type": "test_design_plan",
            "content": update["test_design_plan"],
        }
    # 测试点确认节点：人工改过测试点时随审核结果下发新版测试点（设计策略由前端刷新后取）
    if node_name == "human_review_design" and update.get("test_point_set") is not None:
        return {
            "artifact_type": "test_point_set",
            "content": update["test_point_set"],
        }
    if node_name == "test_case_generation_agent" and update.get("test_cases") is not None:
        return {
            "artifact_type": "test_case_set",
            "content": {
                "test_cases": update["test_cases"],
                "truncated": bool(update.get("cases_truncated")),
                "failed_test_points": update.get("failed_test_points") or [],
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


def _running_conflict(workflow_id: str):
    """进程内已有未结束的运行实例时返回 409 响应，否则返回 None。

    只用 DB 状态做互斥不够：运行收尾期间状态与运行实例会短暂不一致
    （persist_failure 已提交 failed、human_review 重放期间状态回到 waiting_review，
    而 runner 还在发事件、尚未 finish）。这个窗口里再次 start/approve/regenerate
    会通过状态检查、改写状态，但 _start_run 只会返回那个正在收尾的旧实例——
    新任务并不存在，用户提交的确认/修订被静默丢弃。
    """
    run = _run_hub.get(workflow_id)
    if run is not None and not run.done:
        return JSONResponse(
            status_code=409,
            content={"error": "测试工作流正在运行中，请稍后刷新页面查看结果"},
        )
    return None


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
                payload = interrupted_payload or {}
                # 两个确认点（需求分析 / 测试点与设计策略）共用 waiting_review 事件，
                # 前端按 review_type 决定展示哪个面板
                _run_hub.publish(
                    run,
                    {
                        "event": "waiting_review",
                        "review_type": payload.get("type"),
                        "analysis": payload.get("analysis", {}),
                        "test_point_set": payload.get("test_point_set", {}),
                        "test_design_plan": payload.get("test_design_plan", {}),
                    },
                )
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
    """任务列表页（搜索 + 新建入口，点击任务进入详情页）"""
    username = request.session.get("username")
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request, "workflow.html", {"username": username, "user_id": request.session.get("user_id")}
    )


@router.get("/workflow-new", response_class=HTMLResponse)
def workflow_new_page(request: Request):
    """新建任务页：需求原文 + 需求文档上传 + 知识库选择（创建成功后跳详情页自动开始）"""
    username = request.session.get("username")
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request, "workflow_new.html", {"username": username, "user_id": request.session.get("user_id")}
    )


@router.get("/workflow-detail", response_class=HTMLResponse)
def workflow_detail_page(request: Request, workflow_id: str):
    """任务详情页：人工确认、重新生成、用例导出与发布都在这里。

    直接输入不存在/他人的任务 id 时回列表页，不暴露任务是否存在。
    """
    username = request.session.get("username")
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    user_id = request.session.get("user_id")
    workflow = workflow_service.get_owned_workflow(user_id, workflow_id)
    if workflow is None:
        return RedirectResponse(url="/workflows", status_code=303)
    return templates.TemplateResponse(
        request,
        "workflow_detail.html",
        {"username": username, "user_id": user_id, "workflow_id": workflow_id},
    )


@router.post("/api/workflows")
def create_workflow_endpoint(
    data: WorkflowCreate,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    name = (data.name or "").strip() or "新工作流"
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
        requirement_file_name=(data.requirement_file_name or "").strip() or None,
    )
    return _workflow_summary(workflow)


@router.post("/api/workflows/requirement-extract")
async def extract_requirement_document_endpoint(
    file: UploadFile = File(...),
    user_id: int = Depends(require_user),
):
    """需求文档 → 「需求原文」纯文本（不落盘、不落库、不进向量库）。

    前端取得文本后回填需求原文文本框，用户确认/编辑后再创建任务；
    创建接口保持 JSON 契约不变，解析失败在创建之前即暴露给用户。
    """
    try:
        text, truncated, dropped_chars = await knowledge_service.extract_requirement_document(file)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"error": e.detail})

    if not text:
        return JSONResponse(status_code=400, content={"error": "无法从文档中提取到文本内容"})

    return {
        "filename": file.filename,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        # 被剔除的无字形噪声字符数（私用区符号/替换字符/控制符等），前端提示用户
        "dropped_chars": dropped_chars,
    }


@router.get("/api/workflows")
def list_workflows_endpoint(
    keyword: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: int = Depends(require_user),
):
    """任务列表：keyword 匹配任务名与需求原文，limit/offset 分页（不传 limit 则全量）。"""
    workflows, total = workflow_service.search_workflows(
        user_id,
        keyword=(keyword or "").strip() or None,
        limit=limit,
        offset=offset,
    )
    return {
        "workflows": [_workflow_summary(w) for w in workflows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/workflows/{workflow_id}")
def get_workflow_endpoint(workflow_id: str, user_id: int = Depends(require_user)):

    workflow = workflow_service.get_owned_workflow(user_id, workflow_id)
    if not workflow:
        return JSONResponse(status_code=404, content={"error": "工作流不存在"})

    result = _workflow_summary(workflow)
    result["requirement_text"] = workflow.requirement_text
    result["artifacts"] = [_artifact_response(a) for a in workflow_service.get_artifacts(workflow_id)]
    return result


@router.patch("/api/workflows/{workflow_id}")
def rename_workflow_endpoint(
    workflow_id: str,
    data: WorkflowRename,
    user_id: int = Depends(require_user),
):
    """重命名测试工作流（owner-only）：只改名称，不影响需求原文、产物与运行状态。"""
    name = data.name.strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "测试工作流名称不能为空"})

    workflow = workflow_service.rename_workflow(user_id, workflow_id, name)
    if workflow is None:
        return JSONResponse(status_code=404, content={"error": "测试工作流不存在"})
    return _workflow_summary(workflow)


@router.delete("/api/workflows/{workflow_id}")
async def delete_workflow_endpoint(workflow_id: str, user_id: int = Depends(require_user)):
    """删除测试工作流（owner-only）：产物级联删除，checkpoint 线程一并清理。

    运行中的任务不给删：后台执行还在往产物与状态上写，删掉行只会留下一个跑着的
    孤儿任务，且清掉 checkpoint 会让收尾路径报错。等它跑完（或失败）再删。
    """
    async with _run_hub.lock:
        workflow = await asyncio.to_thread(workflow_service.get_owned_workflow, user_id, workflow_id)
        if not workflow:
            return JSONResponse(status_code=404, content={"error": "测试工作流不存在"})
        conflict = _running_conflict(workflow.id)
        if conflict is not None:
            return conflict

        deleted = await asyncio.to_thread(
            workflow_service.delete_workflow, user_id, workflow_id
        )
        if not deleted:
            return JSONResponse(status_code=404, content={"error": "测试工作流不存在"})

    # 清理失败不回滚已删除的任务行：孤儿 checkpoint 只占磁盘、不影响功能
    try:
        await delete_workflow_checkpoints(workflow_id)
    except Exception as e:
        logger.warning("清理测试工作流 %s 的 checkpoint 失败: %s", workflow_id, e, exc_info=True)
    return {"ok": True}


@router.post("/api/workflows/{workflow_id}/start")
async def start_workflow_endpoint(workflow_id: str, user_id: int = Depends(require_user)):
    async with _run_hub.lock:
        workflow = await asyncio.to_thread(workflow_service.get_owned_workflow, user_id, workflow_id)
        if not workflow:
            return JSONResponse(status_code=404, content={"error": "测试工作流不存在"})

        # created/failed：从头跑一轮，用状态机乐观锁互斥（并发 start 只有一个抢占成功）；
        # analyzing/generating：从 checkpoint 续跑（进程重启恢复场景），
        # 已有后台运行时 _start_run 直接返回该运行，本次请求退化为订阅者。
        # waiting_review：图停在 interrupt 上，需要人工确认载荷才能续跑；
        # 以 run_input=None 续跑等价于空 resume（langgraph 边界行为不可靠），明确拒绝
        if workflow.status == "waiting_review":
            return JSONResponse(
                status_code=409,
                content={"error": "测试工作流等待人工确认，请确认后继续，无法重新启动"},
            )
        if workflow.status in ("created", "failed"):
            # 抢占前先确认进程内没有正在收尾的运行实例：否则会"抢到状态但没有新任务"
            conflict = _running_conflict(workflow.id)
            if conflict is not None:
                return conflict
            claimed = await asyncio.to_thread(
                workflow_service.try_claim_workflow,
                workflow.id,
                ("created", "failed"),
                status="analyzing",
                current_step="load_requirement",
                error=None,
            )
            if not claimed:
                return JSONResponse(status_code=409, content={"error": "测试工作流已在运行中，请刷新页面查看"})
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
    """人工确认（两个确认点共用入口，D-036）。

    当前待确认的 gate 由 workflow.current_step 决定（两个 gate 的状态都是 waiting_review）：
    - human_review_design → 期望 test_point_set（None 表示原样确认）
    - 其他（human_review）→ 期望 analysis（None 表示原样确认）
    resume 载荷同时带上两个键，各节点只消费自己的那一个。
    """
    async with _run_hub.lock:
        workflow = await asyncio.to_thread(workflow_service.get_owned_workflow, user_id, workflow_id)
        if not workflow:
            return JSONResponse(status_code=404, content={"error": "测试工作流不存在"})
        # 已有运行实例时不得再次确认：resume 载荷会被静默丢弃
        conflict = _running_conflict(workflow.id)
        if conflict is not None:
            return conflict
        if workflow.status != "waiting_review":
            return JSONResponse(
                status_code=409,
                content={"error": f"测试工作流当前状态为 {workflow.status}，无法确认继续"},
            )

        analysis = data.analysis
        test_point_set = data.test_point_set
        design_gate = workflow.current_step == "human_review_design"

        if design_gate and test_point_set is not None:
            try:
                validated_points = TestPointSet.model_validate(test_point_set)
            except Exception as e:
                return JSONResponse(
                    status_code=422, content={"error": f"修改后的测试点未通过校验：{e}"}
                )
            if not validated_points.test_points:
                return JSONResponse(status_code=422, content={"error": "测试点不能为空"})
            latest_analysis = await asyncio.to_thread(
                workflow_service.get_latest_artifact, workflow_id, "requirement_analysis"
            )
            requirement_ids = set()
            if latest_analysis is not None:
                try:
                    requirement_ids = requirement_ids_from_analysis(json.loads(latest_analysis.content))
                except (TypeError, ValueError):
                    requirement_ids = set()
            invalid = sorted(
                {
                    ref
                    for item in validated_points.test_points
                    for ref in item.requirement_refs
                    if ref not in requirement_ids
                }
            )
            if invalid:
                return JSONResponse(
                    status_code=422,
                    content={"error": "测试点引用了不存在的需求点：" + "、".join(invalid)},
                )
        elif not design_gate and analysis is not None:
            try:
                RequirementAnalysis.model_validate(analysis)
            except Exception as e:
                return JSONResponse(
                    status_code=422, content={"error": f"修改后的需求分析未通过校验：{e}"}
                )

        # resume 载荷恒为非空 dict（langgraph 对 resume=None/空 dict 有边界问题）；
        # 对应键为 None 表示原样确认
        resume_payload = {
            "action": "approve",
            "analysis": analysis,
            "test_point_set": test_point_set,
        }
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
    """重新生成（D-036 两种目标）。

    - target=cases（默认）：从测试点确认之后的 checkpoint 时间旅行重跑用例生成与覆盖校验；
      可携带人工修订的测试点（test_point_set：落新版本 Artifact 并由确定性代码重算设计策略）
      与/或修订后的需求分析。
    - target=test_points：从需求分析确认之后的 checkpoint 重跑测试点设计与设计策略；
      可携带修订后的需求分析。

    D-036 之前的旧任务没有测试点产物：时间旅行无法补齐中间阶段，此时降级为全量重跑一轮
    （保留任务与历史产物），行为等同重新创建任务。
    """
    async with _run_hub.lock:
        workflow = await asyncio.to_thread(workflow_service.get_owned_workflow, user_id, workflow_id)
        if not workflow:
            return JSONResponse(status_code=404, content={"error": "测试工作流不存在"})
        # 同 _running_conflict：避免"写完状态却拿到正在收尾的旧实例"
        conflict = _running_conflict(workflow.id)
        if conflict is not None:
            return conflict
        if workflow.status not in ("completed", "failed"):
            return JSONResponse(
                status_code=409,
                content={"error": f"测试工作流当前状态为 {workflow.status}，无法重新生成"},
            )

        target = (data.target if data is not None else "cases") or "cases"

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

        points_dict = None
        if data is not None and data.test_point_set is not None:
            try:
                validated_points = TestPointSet.model_validate(data.test_point_set)
            except Exception as e:
                return JSONResponse(
                    status_code=422, content={"error": f"修改后的测试点未通过校验：{e}"}
                )
            if not validated_points.test_points:
                return JSONResponse(status_code=422, content={"error": "测试点不能为空"})
            points_dict = validated_points.model_dump()

        # 旧任务（D-036 之前创建）没有测试点产物，时间旅行无法补齐中间阶段 → 全量重跑
        latest_points = await asyncio.to_thread(
            workflow_service.get_latest_artifact, workflow_id, "test_point_set"
        )
        if latest_points is None:
            await asyncio.to_thread(
                workflow_service.update_workflow_status,
                workflow_id,
                status="analyzing",
                current_step="load_requirement",
                error=None,
            )
            run = _start_run(
                workflow_id,
                {"workflow_id": workflow_id, "user_id": user_id, "error": None},
            )
            queue = _run_hub.subscribe(run)
            return StreamingResponse(
                _run_hub.forward_events(run, queue),
                media_type="text/event-stream",
            )

        anchor_node = "test_case_generation_agent" if target == "cases" else "test_point_design_agent"
        graph = await get_compiled_graph()
        target_config = None
        async for snapshot in graph.aget_state_history({"configurable": {"thread_id": workflow_id}}):
            if snapshot.next and snapshot.next[0] == anchor_node:
                target_config = snapshot.config
                break
        if target_config is None:
            return JSONResponse(
                status_code=409,
                content={"error": "未找到可用的检查点，无法重新生成；请重新创建测试工作流"},
            )

        config_extra = {"checkpoint_id": target_config["configurable"]["checkpoint_id"]}
        state_update: dict = {}

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
                state_update["requirement_analysis"] = analysis_dict

        if points_dict is not None:
            latest_points_content = (
                json.loads(latest_points.content) if latest_points.content else {}
            ) or {}
            if (latest_points_content.get("test_points") or []) != points_dict["test_points"]:
                # 测试点被修订：落新版本，并由确定性代码重算设计策略（不接受人工直接编辑策略）
                new_points_content = {
                    "test_points": points_dict["test_points"],
                    "coverage_target": points_dict.get("coverage_target") or "",
                    "notes": points_dict.get("notes") or [],
                    "dropped_test_points": [],
                    "issues": [],
                    "truncated": False,
                }
                await asyncio.to_thread(
                    workflow_service.save_artifact,
                    workflow_id,
                    "test_point_set",
                    new_points_content,
                    latest_points.id,
                )
                plan = build_test_design_plan(points_dict["test_points"])
                latest_design = await asyncio.to_thread(
                    workflow_service.get_latest_artifact, workflow_id, "test_design_plan"
                )
                await asyncio.to_thread(
                    workflow_service.save_artifact,
                    workflow_id,
                    "test_design_plan",
                    plan,
                    latest_design.id if latest_design else None,
                )
                state_update["test_point_set"] = new_points_content
                state_update["test_design_plan"] = plan

        if state_update:
            updated_config = await graph.aupdate_state(target_config, state_update)
            config_extra = {"checkpoint_id": updated_config["configurable"]["checkpoint_id"]}

        await asyncio.to_thread(
            workflow_service.update_workflow_status,
            workflow_id,
            status="generating" if target == "cases" else "analyzing",
            current_step=anchor_node,
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
        return JSONResponse(status_code=404, content={"error": "测试工作流不存在"})

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
