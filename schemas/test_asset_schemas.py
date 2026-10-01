"""测试用例集资产的请求/响应校验模型"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from schemas.workflow_schemas import TestCaseSet


# ---------- API 请求 ----------


class PublishRequest(BaseModel):
    workflow_id: str


class TestCaseSetContentUpdate(BaseModel):
    """全量内容编辑（D-008/D-015）

    base_version 为乐观锁基准；deleted_case_ids 显式声明被删除用例的编号，
    未声明的缺失编号会被服务端拒绝（case_id 不可变）。
    """

    content: TestCaseSet
    base_version: int
    note: Optional[str] = None
    deleted_case_ids: List[str] = Field(default_factory=list)


class TestCaseSetMetaUpdate(BaseModel):
    """元信息编辑（不产生新版本）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    visibility: Optional[Literal["private", "shared"]] = None


class RollbackRequest(BaseModel):
    source_version: int
    note: Optional[str] = None


# ---------- API 响应 ----------


class TestCaseSetResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    visibility: str
    owner_user_id: int
    owner_username: Optional[str] = None
    is_mine: bool = False
    source_workflow_id: Optional[str] = None
    current_version: int
    case_count: int
    created_at: datetime
    updated_at: Optional[datetime] = None


class TestCaseSetVersionResponse(BaseModel):
    id: str
    version: int
    parent_version_id: Optional[str] = None
    source_version_id: Optional[str] = None
    source_type: str
    source_artifact_id: Optional[str] = None
    note: Optional[str] = None
    created_by: Optional[int] = None
    created_at: datetime
    # 列表接口不返回 content，详情接口返回
    content: Optional[dict] = None


class TestCaseSetDiffResponse(BaseModel):
    from_version: int
    to_version: int
    added: List[dict] = Field(default_factory=list)
    removed: List[dict] = Field(default_factory=list)
    changed: List[dict] = Field(default_factory=list)
