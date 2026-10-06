"""API 测试工作台的请求/响应校验模型"""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

ASSERTION_JSON_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")


class ApiSpecCreate(BaseModel):
    """导入 OpenAPI 文档（format 缺省时自适应：先 JSON 后 YAML）"""

    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=500)
    format: Optional[Literal["yaml", "json"]] = None
    content: str = Field(min_length=1, max_length=2_000_000)


class ApiSpecImportUrlRequest(BaseModel):
    """从 URL 导入 OpenAPI 文档（D-022；name 缺省取文档 info.title 或主机名）"""

    url: str = Field(min_length=1, max_length=500)
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=500)


class ApiSpecMetaUpdate(BaseModel):
    """接口文档名称/描述编辑（owner-only，卡片「编辑」入口；至少提供一项）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=500)


class ApiSpecAuthConfig(BaseModel):
    """登录态前置请求配置（D-025）：执行时先发该请求收集 Cookie/Token 供本轮用例携带"""

    method: Literal["get", "post", "put", "patch", "delete"] = "post"
    path: str = Field(min_length=1, max_length=500)
    body: dict = Field(default_factory=dict)
    body_type: Literal["json", "form"] = "json"
    token_field: Optional[str] = Field(default=None, max_length=100)


class ApiSpecAuthUpdate(BaseModel):
    """auth 为 None 表示清除登录态配置"""

    auth: Optional[ApiSpecAuthConfig] = None


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
    source_url: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    # 详情返回接口清单；列表为 None
    endpoints: Optional[List[ApiEndpointResponse]] = None


# ---------- 接口用例（规则引擎 / AI 建议 / 手工） ----------


class ApiCaseAssertion(BaseModel):
    """响应体字段断言（D-027）：点路径 + 操作符 + 期望值

    op=exists 时 expected 忽略（恒存 None）；op=eq/type 时 expected 必填。
    """

    target: str = Field(min_length=1, max_length=200)
    op: Literal["eq", "exists", "type"]
    expected: Any = None

    @model_validator(mode="after")
    def _check_spec(self):
        if any(not part for part in self.target.split(".")):
            raise ValueError("target 必须是非空点路径（如 data.id、data.items.0.name）")
        if self.op == "type" and self.expected not in ASSERTION_JSON_TYPES:
            raise ValueError(f"type 断言的 expected 须为 {'/'.join(ASSERTION_JSON_TYPES)} 之一")
        if self.op in ("eq", "type") and self.expected is None:
            raise ValueError("op=eq/type 时 expected 必填")
        if self.op == "exists":
            self.expected = None
        return self


class ApiCaseItem(BaseModel):
    """单条接口用例（整表替换的行；source_type 随行保留）"""

    name: str = Field(min_length=1, max_length=200)
    request: dict = Field(default_factory=dict)
    expected_status: int = Field(default=200, ge=100, le=599)
    assertions: List[ApiCaseAssertion] = Field(default_factory=list, max_length=50)
    source_type: Literal["rule_engine", "ai", "manual"] = "manual"
    enabled: bool = True


class ApiCasesUpdate(BaseModel):
    cases: List[ApiCaseItem] = Field(default_factory=list, max_length=200)


class ApiCaseAiSuggestRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)


class TestRunCreate(BaseModel):
    """发起一次接口用例执行（base_url 为被测目标，endpoint_ids 缺省=全部启用用例）"""

    base_url: str = Field(min_length=1, max_length=500)
    endpoint_ids: Optional[List[str]] = None


# ---------- AI 业务建议（LLM 结构化输出目标） ----------


class ApiCaseProposal(BaseModel):
    """单条业务异常用例提案"""

    name: str = Field(min_length=1, max_length=200)
    request: dict = Field(default_factory=dict)
    expected_status: int = Field(default=400, ge=100, le=599)
    assertions: List[ApiCaseAssertion] = Field(default_factory=list, max_length=50)


class ApiCaseProposalSet(BaseModel):
    """AI 业务异常用例提案集（openapi_business_cases_workflow 的输出 schema）"""

    proposals: List[ApiCaseProposal] = Field(default_factory=list)
