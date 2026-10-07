
### workflow pydantic 验证
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Literal, Optional


# ---------- LLM 结构化产物（with_structured_output 的目标 schema） ----------


class RequirementItem(BaseModel):
    """单条功能需求点"""

    id: str = Field(description="需求点编号，格式 REQ-001")
    title: str = Field(description="需求点标题")
    description: str = Field(default="", description="需求点说明")


class RiskItem(BaseModel):
    """单条风险"""

    id: str = Field(description="风险编号，格式 RISK-001")
    description: str = Field(description="风险描述")
    level: Literal["high", "medium", "low"] = Field(default="medium", description="风险等级")


class RequirementAnalysis(BaseModel):
    """需求分析节点的结构化产物"""

    summary: str = Field(description="需求一句话概述")
    scope: List[str] = Field(default_factory=list, description="测试范围")
    functional_requirements: List[RequirementItem] = Field(default_factory=list, description="功能需求点列表")
    business_rules: List[str] = Field(default_factory=list, description="业务规则")
    acceptance_criteria: List[str] = Field(default_factory=list, description="验收标准")
    risks: List[RiskItem] = Field(default_factory=list, description="风险列表")
    assumptions: List[str] = Field(default_factory=list, description="假设与待确认项")


class TestCase(BaseModel):
    """单条测试用例"""

    id: str = Field(description="用例编号，格式 TC-[模块]-[序号]")
    title: str = Field(description="测试标题")
    preconditions: List[str] = Field(default_factory=list, description="前置条件")
    steps: List[str] = Field(default_factory=list, description="操作步骤")
    expected_results: List[str] = Field(default_factory=list, description="预期结果")
    priority: Literal["P0", "P1", "P2"] = Field(default="P1", description="优先级")
    automation: Literal["Auto", "Manual"] = Field(default="Manual", description="自动化标记")
    requirement_refs: List[str] = Field(
        default_factory=list, description="覆盖的功能需求点编号（REQ-xxx），必须真实存在"
    )
    rationale: str = Field(default="", description="覆盖说明：该用例验证哪些需求点/业务规则，为什么这样设计")


class TestCaseSet(BaseModel):
    """用例生成节点的结构化产物"""

    test_cases: List[TestCase] = Field(default_factory=list)


class DuplicatePair(BaseModel):
    """疑似重复用例对（确定性计算）"""

    case_a: str = Field(description="用例编号 A")
    case_b: str = Field(description="用例编号 B")
    similarity: float = Field(description="标题相似度 0-1")


class CoverageReport(BaseModel):
    """覆盖检查节点的产物（确定性计算，非 LLM 输出）"""

    total_cases: int = Field(default=0, description="用例总数")
    covered_requirements: List[str] = Field(default_factory=list, description="已被用例覆盖的需求点编号")
    uncovered_requirements: List[str] = Field(default_factory=list, description="未被任何用例覆盖的需求点编号")
    invalid_refs: List[str] = Field(default_factory=list, description="用例引用了不存在的需求点编号")
    priority_summary: dict = Field(default_factory=dict, description="优先级分布，如 {\"P0\": 3}")
    duplicates: List[DuplicatePair] = Field(default_factory=list, description="疑似重复用例对")
    note: str = Field(default="", description="统计口径说明：覆盖基于用例的 requirement_refs 自我声明")


# ---------- API 请求/响应 ----------


class WorkflowCreate(BaseModel):
    name: str = "新任务"
    # 需求正文上限：超长文本会撑爆分析/生成提示词预算，且永久落库
    requirement_text: str = Field(max_length=50_000)
    knowledge_base_id: Optional[str] = None


class ApproveRequest(BaseModel):
    analysis: Optional[dict] = None  # 人工编辑后的需求分析；None 表示原样确认


class RegenerateRequest(BaseModel):
    analysis: Optional[dict] = None  # 重新生成前人工修订的需求分析；None 表示沿用当前版本


class ArtifactResponse(BaseModel):
    id: str
    artifact_type: str
    version: int
    parent_artifact_id: Optional[str] = None
    content: dict
    created_at: datetime


class WorkflowResponse(BaseModel):
    id: str
    name: str
    requirement_text: str
    knowledge_base_id: Optional[str] = None
    status: str
    current_step: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    artifacts: List[ArtifactResponse] = Field(default_factory=list)
