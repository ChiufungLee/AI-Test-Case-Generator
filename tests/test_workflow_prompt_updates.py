"""工作流提示词动态用例上限与覆盖报告口径说明测试"""
from prompts.prompts import get_workflow_prompt_messages
from workflows.nodes import _dynamic_case_limit, build_coverage_report


def test_dynamic_case_limit_clamped():
    assert _dynamic_case_limit(1) == 10    # 下限：需求点很少时保留基础覆盖面
    assert _dynamic_case_limit(5) == 15
    assert _dynamic_case_limit(20) == 40   # 上限：控制输出 token
    assert _dynamic_case_limit(100) == 40


def test_dynamic_case_limit_always_covers_all_requirements():
    """上限必须 >= 需求点数量，否则覆盖报告必然不达标"""
    for count in range(1, 60):
        assert _dynamic_case_limit(count) >= min(count, 40)


def test_generation_prompt_contains_dynamic_case_limit():
    messages = get_workflow_prompt_messages(
        "testcase_generation_workflow", context="", analysis_json="{}", case_limit=18
    )
    system = messages[0].content
    assert "不超过 18 条" in system
    assert "每个功能需求点至少被一条用例覆盖" in system
    assert "不得漏报" in system
    assert "rationale" in system


def test_coverage_report_contains_methodology_note():
    report = build_coverage_report({}, [])
    assert "自我声明" in report["note"]
    assert "rationale" in report["note"]
