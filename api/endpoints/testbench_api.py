"""测试工作台：测试用例集资产页面与 API（发布 / 查看 / 编辑 / 版本 / 回滚 / 导出）"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from schemas.test_asset_schemas import (
    PublishRequest,
    RollbackRequest,
    TestCaseSetContentUpdate,
    TestCaseSetMetaUpdate,
)
from services import test_asset_service
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
