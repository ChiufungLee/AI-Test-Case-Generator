"""五阶段工作流的提示词（D-036）与覆盖报告口径说明测试"""

from prompts.prompts import get_workflow_prompt_messages
from workflows.nodes import build_coverage_report
from workflows.test_design import dynamic_case_budget, dynamic_test_point_limit


def test_dynamic_limits_clamped():
    assert dynamic_test_point_limit(0) == 6    # 下限：需求点很少时保留基础覆盖面
    assert dynamic_test_point_limit(20) == 40  # 上限：控制输出 token
    assert dynamic_case_budget(1) == 10
    assert dynamic_case_budget(30) == 40


def test_analysis_prompt_declares_stage_boundary():
    """需求分析阶段不越界产出测试点/流程细目，新字段齐备"""
    messages = get_workflow_prompt_messages("requirement_analysis_workflow", requirement_text="需求")
    system = messages[0].content
    assert "职责边界" in system
    assert "不要设计测试点" in system
    for field in ("roles", "constraints", "preconditions", "assumptions"):
        assert field in system


def test_test_point_prompt_contains_limit_and_all_dimensions():
    messages = get_workflow_prompt_messages(
        "test_point_design_workflow", analysis_json="{}", test_point_limit=12
    )
    system = messages[0].content
    assert "不超过 12 个" in system
    assert "TP-001" in system
    # 非功能质量属性维度必须显式出现在提示词里（此前的维度清单整体遗漏）
    for dimension in (
        "function",
        "flow",
        "state",
        "permission",
        "data",
        "boundary",
        "exception",
        "business_rule",
        "nonfunctional",
    ):
        assert dimension in system
    assert "不要给出具体测试数据取值" in system


def test_generation_prompt_is_scoped_to_single_test_point():
    messages = get_workflow_prompt_messages(
        "testcase_generation_workflow",
        analysis_json="{}",
        test_point_json="{}",
        design_plan_item_json="{}",
        test_point_id="TP-001",
        case_budget=3,
    )
    system = messages[0].content
    assert "不超过 3 条" in system
    assert "TP-001" in system
    for field in ("test_data", "test_point_refs", "covered_items"):
        assert field in system
    # 编号统一由服务端分配，提示词不再要求模块化编号
    assert "最终编号由系统统一分配" in system


def test_coverage_report_contains_methodology_note():
    report = build_coverage_report({}, [])
    assert "测试点" in report["note"]
    assert "rationale" in report["note"]
