"""覆盖检查节点（确定性代码）与截断 JSON 抢救的单元测试"""

import json

from workflows.nodes import _salvage_truncated_json, build_coverage_report


ANALYSIS = {
    "functional_requirements": [
        {"id": "REQ-001", "title": "验证码登录", "description": ""},
        {"id": "REQ-002", "title": "错误锁定", "description": ""},
        {"id": "REQ-003", "title": "记住登录状态", "description": ""},
    ]
}


def test_coverage_all_covered():
    cases = [
        {"id": "TC-A-001", "title": "验证码登录成功", "priority": "P0", "requirement_refs": ["REQ-001"]},
        {"id": "TC-A-002", "title": "错误锁定", "priority": "P1", "requirement_refs": ["REQ-002"]},
        {"id": "TC-A-003", "title": "记住登录", "priority": "P2", "requirement_refs": ["REQ-003"]},
    ]
    report = build_coverage_report(ANALYSIS, cases)

    assert report["total_cases"] == 3
    assert report["covered_requirements"] == ["REQ-001", "REQ-002", "REQ-003"]
    assert report["uncovered_requirements"] == []
    assert report["invalid_refs"] == []
    assert report["priority_summary"] == {"P0": 1, "P1": 1, "P2": 1}
    assert report["duplicates"] == []


def test_coverage_reports_uncovered_requirements():
    cases = [
        {"id": "TC-A-001", "title": "验证码登录成功", "priority": "P0", "requirement_refs": ["REQ-001"]},
    ]
    report = build_coverage_report(ANALYSIS, cases)

    assert report["covered_requirements"] == ["REQ-001"]
    assert report["uncovered_requirements"] == ["REQ-002", "REQ-003"]


def test_coverage_reports_invalid_refs():
    cases = [
        {
            "id": "TC-A-001",
            "title": "引用了不存在的需求",
            "priority": "P0",
            "requirement_refs": ["REQ-001", "REQ-999"],
        },
    ]
    report = build_coverage_report(ANALYSIS, cases)

    assert report["invalid_refs"] == ["REQ-999"]
    assert report["covered_requirements"] == ["REQ-001"]


def test_coverage_detects_identical_cases():
    """标题与步骤完全一致 → 疑似重复"""
    cases = [
        {
            "id": "TC-A-001",
            "title": "验证码登录成功场景",
            "steps": ["输入手机号", "输入正确验证码", "点击登录"],
            "priority": "P0",
            "requirement_refs": ["REQ-001"],
        },
        {
            "id": "TC-A-002",
            "title": "验证码登录成功场景",
            "steps": ["输入手机号", "输入正确验证码", "点击登录"],
            "priority": "P0",
            "requirement_refs": ["REQ-001"],
        },
    ]
    report = build_coverage_report(ANALYSIS, cases)

    assert len(report["duplicates"]) == 1
    pair = report["duplicates"][0]
    assert {pair["case_a"], pair["case_b"]} == {"TC-A-001", "TC-A-002"}
    assert pair["similarity"] >= 0.85


def test_coverage_no_duplicates_for_reordered_combination_steps():
    """词序不同但语序不同的组合用例不算重复（token_sort_ratio 时代的假阳性）：
    "输入正确用户名错误密码" 与 "输入错误用户名正确密码" 是两个不同的用例"""
    cases = [
        {
            "id": "TC-B-001",
            "title": "正确用户名+错误密码登录",
            "steps": ["输入正确用户名错误密码", "点击登录", "观察结果"],
            "priority": "P0",
            "requirement_refs": ["REQ-001"],
        },
        {
            "id": "TC-B-002",
            "title": "错误用户名+正确密码登录",
            "steps": ["输入错误用户名正确密码", "点击登录", "观察结果"],
            "priority": "P0",
            "requirement_refs": ["REQ-001"],
        },
    ]
    report = build_coverage_report(ANALYSIS, cases)

    assert report["duplicates"] == []


def test_coverage_detects_duplicate_via_steps_despite_reordered_title():
    """标题词序打乱但步骤一致：由步骤相似度判定重复（保序比较下标题单独不够）"""
    cases = [
        {
            "id": "TC-C-001",
            "title": "验证码登录成功场景",
            "steps": ["输入手机号", "输入正确验证码", "点击登录"],
            "priority": "P0",
            "requirement_refs": ["REQ-001"],
        },
        {
            "id": "TC-C-002",
            "title": "登录成功场景验证码",
            "steps": ["输入手机号", "输入正确验证码", "点击登录"],
            "priority": "P0",
            "requirement_refs": ["REQ-001"],
        },
    ]
    report = build_coverage_report(ANALYSIS, cases)

    assert len(report["duplicates"]) == 1


def test_coverage_no_duplicates_for_same_title_different_steps():
    """标题相同但步骤序列不同 → 不同的组合用例，不判重复"""
    cases = [
        {
            "id": "TC-D-001",
            "title": "登录功能验证",
            "steps": ["输入正确用户名", "输入正确密码", "点击登录"],
            "priority": "P0",
            "requirement_refs": ["REQ-001"],
        },
        {
            "id": "TC-D-002",
            "title": "登录功能验证",
            "steps": ["不填写任何内容", "直接点击登录", "观察报错"],
            "priority": "P1",
            "requirement_refs": ["REQ-001"],
        },
    ]
    report = build_coverage_report(ANALYSIS, cases)

    assert report["duplicates"] == []


def test_coverage_handles_empty_inputs():
    report = build_coverage_report({}, [])

    assert report["total_cases"] == 0
    assert report["covered_requirements"] == []
    assert report["uncovered_requirements"] == []
    assert report["priority_summary"] == {}


def test_salvage_truncated_case_set_keeps_complete_cases():
    truncated = (
        '{"test_cases":['
        '{"id":"TC-A-001","title":"用例A","steps":["s1","s2"]},'
        '{"id":"TC-A-002","title":"用例B","steps":["s3"]},'
        '{"id":"TC-A-003","title":"用例C","steps":["s4'
    )
    salvaged = _salvage_truncated_json(truncated)

    assert salvaged is not None
    data = json.loads(salvaged)
    assert [c["id"] for c in data["test_cases"]] == ["TC-A-001", "TC-A-002"]


def test_salvage_handles_brackets_inside_strings():
    truncated = (
        '{"test_cases":['
        '{"id":"TC-A-001","title":"含括号的标题 [示例]","steps":["s1"]},'
        '{"id":"TC-A-002","title":"用例B'
    )
    salvaged = _salvage_truncated_json(truncated)

    assert salvaged is not None
    data = json.loads(salvaged)
    assert data["test_cases"][0]["title"] == "含括号的标题 [示例]"


def test_salvage_returns_none_for_complete_json():
    assert _salvage_truncated_json('{"test_cases": []}') is None


def test_salvage_returns_none_when_no_complete_object():
    assert _salvage_truncated_json('{"test_cases":[{"id":"TC-A-00') is None
