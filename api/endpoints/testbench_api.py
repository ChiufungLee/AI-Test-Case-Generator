"""测试工作台：测试用例集资产页面与 API（发布 / 查看 / 编辑 / 版本 / 回滚 / 导出）"""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from schemas.api_test_schemas import (
    ApiCaseAiSuggestRequest,
    ApiCasesUpdate,
    ApiSpecAuthUpdate,
    ApiSpecCreate,
    ApiSpecImportUrlRequest,
    ApiSpecMetaUpdate,
    TestRunCreate,
)
from schemas.test_asset_schemas import (
    AIEditConfirmRequest,
    AIEditPreviewRequest,
    PublishRequest,
    RollbackRequest,
    TestCaseSetContentUpdate,
    TestCaseSetMetaUpdate,
)
from api.endpoints.run_hub import RunHub
from services import api_case_service, api_spec_service, test_asset_service, test_run_service
from services.auth_service import require_user
from utils.data_handle import testcases_to_csv

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
logger = logging.getLogger(__name__)


def _error(status_code: int, message) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": str(message)})


# ---------- 页面 ----------


@router.get("/testbench", response_class=HTMLResponse)
def testbench_page(request: Request):
    username = request.session.get("username")
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    user_id = request.session.get("user_id")
    return templates.TemplateResponse(
        request, "testbench.html", {"username": username, "user_id": user_id}
    )


@router.get("/api-test", response_class=HTMLResponse)
def api_test_page(request: Request):
    """接口测试页（D-026：原测试工作台的 API 接口管理拆分为独立一级菜单）"""
    username = request.session.get("username")
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    user_id = request.session.get("user_id")
    return templates.TemplateResponse(
        request, "api_test.html", {"username": username, "user_id": user_id}
    )


@router.get("/testbench-detail", response_class=HTMLResponse)
def testbench_detail_page(request: Request, set_id: str):
    username = request.session.get("username")
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    user_id = request.session.get("user_id")
    asset = test_asset_service.get_test_set(set_id, user_id)
    if asset is None:
        return RedirectResponse(url="/testbench", status_code=303)
    return templates.TemplateResponse(
        request,
        "testbench_detail.html",
        {
            "username": username,
            "user_id": user_id,
            "set_id": set_id,
            "can_edit": asset.owner_user_id == user_id,
        },
    )


# ---------- API ----------


@router.post("/api/test-sets/publish")
def publish_test_set(data: PublishRequest, user_id: int = Depends(require_user)):
    try:
        asset, version = test_asset_service.publish_from_workflow(user_id, data.workflow_id)
    except test_asset_service.NotFoundError as e:
        return _error(404, e)
    except test_asset_service.ConflictError as e:
        return _error(409, e)
    except ValueError as e:
        return _error(422, e)

    return {
        "test_set": test_asset_service.asset_payload(
            asset, owner_username=test_asset_service.get_username(asset.owner_user_id), is_mine=True
        ),
        "version": test_asset_service.version_payload(version, include_content=False),
    }


@router.get("/api/test-sets")
def list_test_sets(user_id: int = Depends(require_user)):
    return test_asset_service.list_test_sets(user_id)


@router.get("/api/test-sets/{set_id}")
def get_test_set_detail(set_id: str, user_id: int = Depends(require_user)):
    view = test_asset_service.get_test_set_view(set_id, user_id)
    if view is None:
        return _error(404, "测试用例集不存在")
    return view


@router.patch("/api/test-sets/{set_id}")
def update_test_set_meta(set_id: str, data: TestCaseSetMetaUpdate, user_id: int = Depends(require_user)):
    try:
        asset = test_asset_service.update_meta(
            set_id, user_id,
            name=data.name, description=data.description, visibility=data.visibility,
        )
    except test_asset_service.NotFoundError as e:
        return _error(404, e)
    except PermissionError as e:
        return _error(403, e)

    return test_asset_service.asset_payload(
        asset, owner_username=test_asset_service.get_username(asset.owner_user_id), is_mine=True
    )


@router.put("/api/test-sets/{set_id}/content")
def update_test_set_content(set_id: str, data: TestCaseSetContentUpdate, user_id: int = Depends(require_user)):
    try:
        version = test_asset_service.save_new_version(
            set_id,
            user_id,
            content=data.content.model_dump(),
            base_version=data.base_version,
            source_type="manual_edit",
            note=data.note,
            deleted_case_ids=data.deleted_case_ids,
        )
        asset = test_asset_service.get_test_set(set_id, user_id)
    except test_asset_service.NotFoundError as e:
        return _error(404, e)
    except PermissionError as e:
        return _error(403, e)
    except test_asset_service.ConflictError as e:
        return _error(409, e)
    except ValueError as e:
        return _error(422, e)

    return {
        "test_set": test_asset_service.asset_payload(asset, is_mine=True),
        "version": test_asset_service.version_payload(version, include_content=False),
    }


@router.get("/api/test-sets/{set_id}/versions")
def list_test_set_versions(set_id: str, user_id: int = Depends(require_user)):
    if test_asset_service.get_test_set(set_id, user_id) is None:
        return _error(404, "测试用例集不存在")
    return test_asset_service.list_version_views(set_id)


@router.get("/api/test-sets/{set_id}/versions/{version}")
def get_test_set_version(set_id: str, version: int, user_id: int = Depends(require_user)):
    if test_asset_service.get_test_set(set_id, user_id) is None:
        return _error(404, "测试用例集不存在")
    view = test_asset_service.get_version_view(set_id, version)
    if view is None:
        return _error(404, "版本不存在")
    return view


@router.get("/api/test-sets/{set_id}/diff")
def diff_test_set_versions(
    set_id: str,
    from_version: int = Query(...),
    to_version: int = Query(...),
    user_id: int = Depends(require_user),
):
    if test_asset_service.get_test_set(set_id, user_id) is None:
        return _error(404, "测试用例集不存在")
    try:
        return test_asset_service.diff_versions(set_id, from_version, to_version)
    except test_asset_service.NotFoundError as e:
        return _error(404, e)


@router.post("/api/test-sets/{set_id}/rollback")
def rollback_test_set(set_id: str, data: RollbackRequest, user_id: int = Depends(require_user)):
    try:
        version = test_asset_service.rollback_version(
            set_id, user_id, data.source_version, note=data.note
        )
    except test_asset_service.NotFoundError as e:
        return _error(404, e)
    except PermissionError as e:
        return _error(403, e)
    except test_asset_service.ConflictError as e:
        return _error(409, e)

    return {"version": test_asset_service.version_payload(version, include_content=False)}


@router.post("/api/test-sets/{set_id}/ai-edit/preview")
async def ai_edit_preview_endpoint(set_id: str, data: AIEditPreviewRequest, user_id: int = Depends(require_user)):
    try:
        return await test_asset_service.ai_edit_preview(set_id, user_id, data.instruction)
    except test_asset_service.NotFoundError as e:
        return _error(404, e)
    except PermissionError as e:
        return _error(403, e)
    except test_asset_service.AIEditError:
        # LLM 与解析细节只在服务层日志留痕，对外统一通用文案（遵守错误文案不落库约定）
        return _error(502, "AI 修改失败，请稍后重试")


@router.post("/api/test-sets/{set_id}/ai-edit/confirm")
def ai_edit_confirm_endpoint(set_id: str, data: AIEditConfirmRequest, user_id: int = Depends(require_user)):
    try:
        asset, version = test_asset_service.ai_edit_confirm(
            set_id, user_id, data.content.model_dump(), data.base_version, data.note,
        )
    except test_asset_service.NotFoundError as e:
        return _error(404, e)
    except PermissionError as e:
        return _error(403, e)
    except test_asset_service.ConflictError as e:
        return _error(409, e)
    except ValueError as e:
        return _error(422, e)

    return {
        "test_set": test_asset_service.asset_payload(
            asset, owner_username=test_asset_service.get_username(asset.owner_user_id), is_mine=True
        ),
        "version": test_asset_service.version_payload(version, include_content=False),
    }


@router.get("/api/test-sets/{set_id}/export")
def export_test_set_csv(set_id: str, user_id: int = Depends(require_user)):
    asset = test_asset_service.get_test_set(set_id, user_id)
    if asset is None:
        return _error(404, "测试用例集不存在")

    version = test_asset_service.get_version(set_id, asset.current_version)
    if version is None:
        return _error(404, "未找到用例版本")

    try:
        content = json.loads(version.content)
    except (TypeError, ValueError):
        return _error(500, "用例集内容损坏")

    csv_data = testcases_to_csv(content.get("test_cases") or [])
    headers = {
        "Content-Disposition": f"attachment; filename=test_set_{set_id}_v{version.version}.csv",
        "Content-Type": "text/csv; charset=utf-8",
    }
    # utf-8-sig 带 BOM，保证中文在 Excel 中不乱码
    return Response(content=csv_data.encode("utf-8-sig"), headers=headers)


@router.delete("/api/test-sets/{set_id}")
def delete_test_set(set_id: str, user_id: int = Depends(require_user)):
    try:
        test_asset_service.delete_test_set(set_id, user_id)
    except test_asset_service.NotFoundError as e:
        return _error(404, e)
    except PermissionError as e:
        return _error(403, e)
    return {"ok": True}


# ---------- API 接口管理（OpenAPI 导入：粘贴 / URL，D-022） ----------


@router.post("/api/api-specs")
def create_api_spec_endpoint(data: ApiSpecCreate, user_id: int = Depends(require_user)):
    try:
        spec = api_spec_service.create_api_spec(
            user_id, data.name, data.content, data.format, description=data.description
        )
    except ValueError as e:
        return _error(422, e)
    return api_spec_service.spec_payload(
        spec, owner_username=api_spec_service.get_username(spec.owner_user_id), is_mine=True
    )


@router.post("/api/api-specs/import-url")
def import_api_spec_from_url_endpoint(data: ApiSpecImportUrlRequest, user_id: int = Depends(require_user)):
    try:
        spec = api_spec_service.create_api_spec_from_url(user_id, data.url, data.name, data.description)
    except ValueError as e:
        return _error(422, e)
    return api_spec_service.spec_payload(
        spec, owner_username=api_spec_service.get_username(spec.owner_user_id), is_mine=True
    )


@router.patch("/api/api-specs/{spec_id}")
def update_api_spec_meta_endpoint(spec_id: str, data: ApiSpecMetaUpdate, user_id: int = Depends(require_user)):
    """编辑接口文档名称/描述（owner-only，D-026 后卡片「编辑」入口）"""
    try:
        spec = api_spec_service.update_api_spec_meta(spec_id, user_id, data.name, data.description)
    except api_spec_service.NotFoundError as e:
        return _error(404, e)
    except ValueError as e:
        return _error(422, e)
    return api_spec_service.spec_payload(
        spec, owner_username=api_spec_service.get_username(spec.owner_user_id), is_mine=True
    )


@router.post("/api/api-specs/{spec_id}/sync")
def sync_api_spec_endpoint(spec_id: str, user_id: int = Depends(require_user)):
    try:
        spec = api_spec_service.sync_api_spec(spec_id, user_id)
    except api_spec_service.NotFoundError as e:
        return _error(404, e)
    except ValueError as e:
        return _error(422, e)
    return api_spec_service.spec_payload(
        spec, owner_username=api_spec_service.get_username(spec.owner_user_id), is_mine=True
    )


@router.put("/api/api-specs/{spec_id}/auth")
def set_api_spec_auth_endpoint(spec_id: str, data: ApiSpecAuthUpdate, user_id: int = Depends(require_user)):
    """设置/清除登录态前置请求配置（D-025，owner-only）"""
    try:
        auth = api_spec_service.set_auth_config(
            spec_id, user_id, data.auth.model_dump() if data.auth else None
        )
    except api_spec_service.NotFoundError as e:
        return _error(404, e)
    except ValueError as e:
        return _error(422, e)
    return {"auth": auth}


@router.get("/api/api-specs")
def list_api_specs_endpoint(user_id: int = Depends(require_user)):
    return api_spec_service.list_api_specs(user_id)


@router.get("/api/api-specs/{spec_id}")
def get_api_spec_endpoint(spec_id: str, user_id: int = Depends(require_user)):
    view = api_spec_service.get_api_spec_view(spec_id, user_id)
    if view is None:
        return _error(404, "接口文档不存在")
    return view


@router.delete("/api/api-specs/{spec_id}")
def delete_api_spec_endpoint(spec_id: str, user_id: int = Depends(require_user)):
    try:
        api_spec_service.delete_api_spec(spec_id, user_id)
    except api_spec_service.NotFoundError as e:
        return _error(404, e)
    except PermissionError as e:
        return _error(403, e)
    return {"ok": True}


# ---------- 接口用例（规则引擎 / AI 建议 / 手工） ----------


@router.get("/api/api-specs/{spec_id}/endpoints/{endpoint_id}/cases")
def list_endpoint_cases_endpoint(spec_id: str, endpoint_id: str, user_id: int = Depends(require_user)):
    try:
        return api_case_service.list_cases(spec_id, endpoint_id, user_id)
    except api_case_service.NotFoundError as e:
        return _error(404, e)


@router.post("/api/api-specs/{spec_id}/endpoints/{endpoint_id}/cases/generate")
def generate_endpoint_cases_endpoint(spec_id: str, endpoint_id: str, user_id: int = Depends(require_user)):
    try:
        return api_case_service.generate_cases(spec_id, endpoint_id, user_id)
    except api_case_service.NotFoundError as e:
        return _error(404, e)
    except ValueError as e:
        return _error(422, e)


@router.post("/api/api-specs/{spec_id}/endpoints/{endpoint_id}/cases/ai-suggest")
async def ai_suggest_endpoint_cases_endpoint(
    spec_id: str,
    endpoint_id: str,
    data: ApiCaseAiSuggestRequest,
    user_id: int = Depends(require_user),
):
    try:
        return await api_case_service.ai_suggest_cases(spec_id, endpoint_id, user_id, data.instruction)
    except api_case_service.NotFoundError as e:
        return _error(404, e)
    except ValueError as e:
        return _error(422, e)
    except api_case_service.AISuggestError:
        # LLM 与解析细节只在服务层日志留痕，对外统一通用文案
        return _error(502, "AI 建议生成失败，请稍后重试")


@router.put("/api/api-specs/{spec_id}/endpoints/{endpoint_id}/cases")
def update_endpoint_cases_endpoint(
    spec_id: str,
    endpoint_id: str,
    data: ApiCasesUpdate,
    user_id: int = Depends(require_user),
):
    try:
        return api_case_service.save_cases(
            spec_id, endpoint_id, user_id, [case.model_dump() for case in data.cases]
        )
    except api_case_service.NotFoundError as e:
        return _error(404, e)
    except ValueError as e:
        return _error(422, e)


# ---------- 测试执行（进程内 httpx，D-012；SSE 基建与工作流共用 RunHub，D-021） ----------

_run_hub = RunHub()


@router.post("/api/api-specs/{spec_id}/runs")
async def start_test_run_endpoint(spec_id: str, data: TestRunCreate, user_id: int = Depends(require_user)):
    async with _run_hub.lock:
        try:
            run = await asyncio.to_thread(
                test_run_service.try_claim_run, spec_id, user_id, data.base_url, data.endpoint_ids
            )
        except test_run_service.NotFoundError as e:
            return _error(404, e)
        except test_run_service.ConflictError as e:
            return _error(409, e)
        except ValueError as e:
            return _error(422, e)

        if run is None:
            # 本人已有运行中：转为订阅既有运行
            running = await asyncio.to_thread(test_run_service.get_running_run, spec_id, user_id)
            handle = _run_hub.get(running.id) if running else None
            if handle is None:
                # 进程重启后的残留 running（进程内无对应运行）：提示重试
                return _error(409, "该文档已有执行中的运行（进程重启残留），请稍后重试")
            queue = _run_hub.subscribe(handle)
            return StreamingResponse(_run_hub.forward_events(handle, queue), media_type="text/event-stream")

        handle = _run_hub.register(
            run.id,
            lambda: asyncio.create_task(
                test_run_service.execute_run(run.id, data.base_url, data.endpoint_ids, _run_hub)
            ),
        )
    queue = _run_hub.subscribe(handle)
    return StreamingResponse(_run_hub.forward_events(handle, queue), media_type="text/event-stream")


@router.get("/api/test-runs/{run_id}")
def get_test_run_endpoint(run_id: str, user_id: int = Depends(require_user)):
    view = test_run_service.get_run_view(run_id, user_id)
    if view is None:
        return _error(404, "执行记录不存在")
    return view


@router.get("/api/api-specs/{spec_id}/runs")
def list_test_runs_endpoint(spec_id: str, user_id: int = Depends(require_user)):
    try:
        return test_run_service.list_runs(spec_id, user_id)
    except test_run_service.NotFoundError as e:
        return _error(404, e)
