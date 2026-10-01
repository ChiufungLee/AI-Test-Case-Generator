"""API 测试工作台的请求/响应校验模型"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ApiSpecCreate(BaseModel):
    """导入 OpenAPI 文档（format 缺省时自适应：先 JSON 后 YAML）"""

    name: str = Field(min_length=1, max_length=200)
    format: Optional[Literal["yaml", "json"]] = None
    content: str = Field(min_length=1, max_length=2_000_000)


class ApiEndpointResponse(BaseModel):
    id: str
    method: str
    path: str
    operation_id: str = ""
    summary: str = ""
    parameters: List[dict] = Field(default_factory=list)
    request_body: Optional[dict] = None


class ApiSpecResponse(BaseModel):
    id: str
    name: str
    format: str
    spec_title: str = ""
    spec_version: str = ""
    endpoint_count: int
    visibility: str
    owner_user_id: int
    owner_username: Optional[str] = None
    is_mine: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None
    # 详情返回接口清单；列表为 None
    endpoints: Optional[List[ApiEndpointResponse]] = None


# ---------- 接口用例（规则引擎 / AI 建议 / 手工） ----------


class ApiCaseItem(BaseModel):
    """单条接口用例（整表替换的行；source_type 随行保留）"""

    name: str = Field(min_length=1, max_length=200)
    request: dict = Field(default_factory=dict)
    expected_status: int = Field(default=200, ge=100, le=599)
    source_type: Literal["rule_engine", "ai", "manual"] = "manual"
    enabled: bool = True


class ApiCasesUpdate(BaseModel):
    cases: List[ApiCaseItem] = Field(default_factory=list, max_length=200)


class ApiCaseAiSuggestRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)


# ---------- AI 业务建议（LLM 结构化输出目标） ----------


class ApiCaseProposal(BaseModel):
    """单条业务异常用例提案"""

    name: str = Field(min_length=1, max_length=200)
    request: dict = Field(default_factory=dict)
    expected_status: int = Field(default=400, ge=100, le=599)


class ApiCaseProposalSet(BaseModel):
    """AI 业务异常用例提案集（openapi_business_cases_workflow 的输出 schema）"""

    proposals: List[ApiCaseProposal] = Field(default_factory=list)
