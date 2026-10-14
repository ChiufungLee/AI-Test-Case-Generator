
### workflow pydantic 验证
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import List, Literal, Optional


def _coerce_id_text(value):
    """编号字段的类型收敛：LLM 常把「批内临时序号」输出成数字（1/2/3），
    统一转成字符串再校验。

    只做 int/float → str，不留空值兜底：编号仍是必填项（TestCaseSet 同时用于
    测试用例集资产的内容校验，D-015 把 case_id 当不可变业务身份，不能容忍空编号）。
    工作流侧的用例编号最终由 workflows.test_design.normalize_case_ids 统一分配。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return value


# ---------- LLM 结构化产物（with_structured_output 的目标 schema） ----------


class RequirementItem(BaseModel):
    """单条功能需求点"""

    id: str = Field(description="需求点编号，格式 REQ-001")
    title: str = Field(description="需求点标题")
    description: str = Field(default="", description="需求点说明")

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value):
        return _coerce_id_text(value)


class RiskItem(BaseModel):
    """单条风险"""

    id: str = Field(description="风险编号，格式 RISK-001")
    description: str = Field(description="风险描述")
    level: Literal["high", "medium", "low"] = Field(default="medium", description="风险等级")

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value):
        return _coerce_id_text(value)


class RequirementAnalysis(BaseModel):
    """需求分析节点的结构化产物（D-036 阶段 1：测试依据分析与缺陷识别）"""

    summary: str = Field(description="需求一句话概述")
    scope: List[str] = Field(default_factory=list, description="测试范围")
    roles: List[str] = Field(default_factory=list, description="涉及的角色/用户类型")
    functional_requirements: List[RequirementItem] = Field(default_factory=list, description="功能需求点列表")
    business_rules: List[str] = Field(default_factory=list, description="业务规则")
    constraints: List[str] = Field(default_factory=list, description="约束与限制（技术/环境/合规）")
    preconditions: List[str] = Field(default_factory=list, description="业务前置条件（需求级）")
    acceptance_criteria: List[str] = Field(default_factory=list, description="验收标准")
    risks: List[RiskItem] = Field(default_factory=list, description="风险列表")
    assumptions: List[str] = Field(default_factory=list, description="歧义、冲突与待确认项")


# ---------- 五阶段工作流的中间产物（D-036） ----------

# 测试点覆盖维度（阶段 2）：非功能维度是此前缺失的一环（ISO 25010 / MFQ 的 QC）
TEST_POINT_DIMENSIONS = (
    "function",
    "flow",
    "state",
    "permission",
    "data",
    "boundary",
    "exception",
    "business_rule",
    "nonfunctional",
)

# 测试设计技术（阶段 3）：技术适用性由确定性规则判定，不再交给 LLM
TEST_DESIGN_TECHNIQUES = (
    "equivalence_partitioning",
    "boundary_value",
    "decision_table",
    "state_transition",
    "scenario",
    "error_guessing",
    "exploratory",
)


class TestPoint(BaseModel):
    """单个测试点（阶段 2 产物）：只回答"测什么"，不产出取值、步骤与预期"""

    id: str = Field(description="测试点编号，格式 TP-001（字符串）")
    title: str = Field(description="测试点标题")
    description: str = Field(default="", description="要验证的可测方面")
    requirement_refs: List[str] = Field(
        default_factory=list, description="关联的功能需求点编号（REQ-xxx），必须真实存在"
    )
    dimension: Literal[
        "function",
        "flow",
        "state",
        "permission",
        "data",
        "boundary",
        "exception",
        "business_rule",
        "nonfunctional",
    ] = Field(default="function", description="覆盖维度")
    risk_level: Literal["high", "medium", "low"] = Field(default="medium", description="风险等级")
    priority: Literal["P0", "P1", "P2"] = Field(default="P1", description="优先级")
    rationale: str = Field(default="", description="为什么需要这个测试点")

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value):
        return _coerce_id_text(value)


class TestPointSet(BaseModel):
    """测试点设计节点的结构化产物（LLM 输出目标；丢弃与裁剪结果由节点追加）"""

    test_points: List[TestPoint] = Field(default_factory=list)
    coverage_target: str = Field(default="", description="本轮覆盖目标说明")
    notes: List[str] = Field(default_factory=list, description="覆盖取舍说明")


class CoverageItem(BaseModel):
    """覆盖项（阶段 3 确定性产物）：技术推导出的"必须被覆盖的方面"，不含具体取值"""

    id: str = Field(description="覆盖项编号，格式 CI-TP001-01")
    test_point_id: str = Field(description="所属测试点编号")
    technique: Literal[
        "equivalence_partitioning",
        "boundary_value",
        "decision_table",
        "state_transition",
        "scenario",
        "error_guessing",
        "exploratory",
    ] = Field(description="推导该覆盖项的技术")
    description: str = Field(description="覆盖项说明")
    data_hint: str = Field(default="", description="待填测试数据的描述（不含具体取值）")


class TestDesignPlanItem(BaseModel):
    """单个测试点的设计策略"""

    test_point_id: str = Field(description="测试点编号")
    techniques: List[
        Literal[
            "equivalence_partitioning",
            "boundary_value",
            "decision_table",
            "state_transition",
            "scenario",
            "error_guessing",
            "exploratory",
        ]
    ] = Field(default_factory=list, description="选定的测试技术")
    coverage_items: List[CoverageItem] = Field(default_factory=list, description="覆盖项清单")
    case_budget: int = Field(default=1, description="该测试点的用例预算")
    rationale: str = Field(default="", description="技术选型理由")


class TestDesignPlan(BaseModel):
    """测试设计策略节点的产物（确定性计算，非 LLM 输出）"""

    items: List[TestDesignPlanItem] = Field(default_factory=list)
    total_budget: int = Field(default=0, description="用例总预算")
    technique_summary: dict = Field(default_factory=dict, description="技术分布，如 {\"boundary_value\": 3}")
    notes: List[str] = Field(default_factory=list, description="预算与裁剪说明")


class TestCase(BaseModel):
    """单条测试用例"""

    id: str = Field(description="用例编号（字符串；本阶段批内临时序号即可，「1」「2」…）")
    title: str = Field(description="测试标题")
    preconditions: List[str] = Field(default_factory=list, description="前置条件")
    steps: List[str] = Field(default_factory=list, description="操作步骤")
    test_data: str = Field(default="", description="测试数据（多行文本；独立成字段以便确定性检查与导出）")
    expected_results: List[str] = Field(default_factory=list, description="预期结果")
    priority: Literal["P0", "P1", "P2"] = Field(default="P1", description="优先级")
    automation: Literal["Auto", "Manual"] = Field(default="Manual", description="自动化标记")
    requirement_refs: List[str] = Field(
        default_factory=list, description="覆盖的功能需求点编号（REQ-xxx），必须真实存在"
    )
    test_point_refs: List[str] = Field(
        default_factory=list, description="覆盖的测试点编号（TP-xxx），必须真实存在"
    )
    covered_items: List[str] = Field(
        default_factory=list, description="覆盖的覆盖项编号（CI-xxx）"
    )
    rationale: str = Field(default="", description="覆盖说明：该用例验证哪些需求点/业务规则，为什么这样设计")

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value):
        return _coerce_id_text(value)


class TestCaseSet(BaseModel):
    """用例生成节点的结构化产物"""

    test_cases: List[TestCase] = Field(default_factory=list)


class DuplicatePair(BaseModel):
    """疑似重复用例对（确定性计算）"""

    case_a: str = Field(description="用例编号 A")
    case_b: str = Field(description="用例编号 B")
    similarity: float = Field(description="标题相似度 0-1")


class CoverageReport(BaseModel):
    """覆盖校验与质检节点的产物（确定性计算，非 LLM 输出；D-036 改为对照测试点/覆盖项清单核算）"""

    total_cases: int = Field(default=0, description="用例总数")
    total_test_points: int = Field(default=0, description="测试点总数")
    covered_test_points: List[str] = Field(default_factory=list, description="已被用例覆盖的测试点编号")
    uncovered_test_points: List[str] = Field(default_factory=list, description="未被任何用例覆盖的测试点编号")
    invalid_test_point_refs: List[str] = Field(default_factory=list, description="用例引用了不存在的测试点编号")
    uncovered_coverage_items: List[str] = Field(default_factory=list, description="没有任何用例声明的覆盖项编号")
    invalid_covered_items: List[str] = Field(default_factory=list, description="用例引用了不存在的覆盖项编号")
    covered_requirements: List[str] = Field(default_factory=list, description="已被用例覆盖的需求点编号")
    uncovered_requirements: List[str] = Field(default_factory=list, description="未被任何用例覆盖的需求点编号")
    invalid_refs: List[str] = Field(default_factory=list, description="用例引用了不存在的需求点编号")
    dimension_summary: dict = Field(default_factory=dict, description="测试点维度分布，如 {\"boundary\": 3}")
    priority_summary: dict = Field(default_factory=dict, description="优先级分布，如 {\"P0\": 3}")
    duplicates: List[DuplicatePair] = Field(default_factory=list, description="疑似重复用例对")
    duplicate_test_points: List[DuplicatePair] = Field(default_factory=list, description="疑似重复测试点对")
    quality_issues: List[str] = Field(default_factory=list, description="质量问题清单（字段完整性等确定性检查）")
    failed_test_points: List[str] = Field(default_factory=list, description="用例生成失败（批次异常）的测试点")
    note: str = Field(default="", description="统计口径说明：覆盖依据测试点与覆盖项清单核算，非用例自我声明")


# ---------- API 请求/响应 ----------


class WorkflowCreate(BaseModel):
    name: str = "新任务"
    # 需求正文上限：超长文本会撑爆分析/生成提示词预算，且永久落库
    requirement_text: str = Field(max_length=50_000)
    knowledge_base_id: Optional[str] = None
    # 需求原文的来源文档名（经需求文档上传回填时携带；手工粘贴为空）
    requirement_file_name: Optional[str] = Field(default=None, max_length=255)


class WorkflowRename(BaseModel):
    """重命名测试工作流（只改名称，不动需求原文与产物）"""

    # 与 Workflow.name 的列宽一致（String(200)）；空串/纯空白由端点 strip 后再判空
    name: str = Field(min_length=1, max_length=200)


class ApproveRequest(BaseModel):
    """人工确认载荷：两个确认点共用同一入口，各自消费自己的键（D-036）

    analysis        —— 需求分析确认点（human_review）
    test_point_set  —— 测试点与设计策略确认点（human_review_design）
    为 None 均表示原样确认。
    """

    analysis: Optional[dict] = None
    test_point_set: Optional[dict] = None


class RegenerateRequest(BaseModel):
    """重新生成请求（D-036）

    target=cases       从测试点确认之后重跑用例生成与覆盖校验（保留测试点）
    target=test_points 从需求分析确认之后重跑测试点设计（连带设计策略）
    """

    target: Literal["cases", "test_points"] = "cases"
    analysis: Optional[dict] = None
    test_point_set: Optional[dict] = None


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
