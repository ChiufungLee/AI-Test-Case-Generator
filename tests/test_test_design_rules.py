"""测试设计确定性规则（workflows/test_design.py）的单元测试（D-036 阶段 3）。"""

from workflows.test_design import (
    CASE_BATCH_SIZE,
    allocate_case_budget,
    build_coverage_items,
    build_test_design_plan,
    case_quality_issues,
    case_similarity,
    detect_techniques,
    dynamic_case_budget,
    dynamic_test_point_limit,
    find_duplicate_test_points,
    normalize_case_ids,
    requirement_ids_from_analysis,
    short_code,
    validate_test_points,
)

ANALYSIS = {
    "functional_requirements": [
        {"id": "REQ-001", "title": "验证码登录", "description": ""},
        {"id": "REQ-002", "title": "错误锁定", "description": ""},
    ]
}


def _point(point_id, title, requirements=("REQ-001",), **kwargs):
    return {
        "id": point_id,
        "title": title,
        "description": kwargs.pop("description", ""),
        "requirement_refs": list(requirements),
        "dimension": kwargs.pop("dimension", "function"),
        "risk_level": kwargs.pop("risk_level", "medium"),
        "priority": kwargs.pop("priority", "P1"),
        "rationale": kwargs.pop("rationale", ""),
        **kwargs,
    }


# ---------- 规模与工具函数 ----------


def test_dynamic_test_point_limit_clamps():
    assert dynamic_test_point_limit(0) == 6
    assert dynamic_test_point_limit(2) == 6
    assert dynamic_test_point_limit(5) == 15
    assert dynamic_test_point_limit(50) == 40


def test_dynamic_case_budget_clamps():
    assert dynamic_case_budget(0) == 10
    assert dynamic_case_budget(3) == 10
    assert dynamic_case_budget(8) == 16
    assert dynamic_case_budget(100) == 40


def test_requirement_ids_from_analysis_ignores_items_without_id():
    analysis = {
        "functional_requirements": [
            {"id": "REQ-001"},
            {"title": "无编号需求"},
            {"id": "REQ-002"},
        ]
    }
    assert requirement_ids_from_analysis(analysis) == {"REQ-001", "REQ-002"}
    assert requirement_ids_from_analysis({}) == set()


def test_short_code_strips_separators():
    assert short_code("TP-001") == "TP001"
    assert short_code("") == "TP"


def test_case_similarity_is_order_sensitive():
    """保序比较：语序不同的组合用例不能被判为重复（token_sort_ratio 时代的假阳性）"""
    a = {"title": "正确用户名+错误密码", "steps": ["输入正确用户名错误密码"]}
    b = {"title": "错误用户名+正确密码", "steps": ["输入错误用户名正确密码"]}
    assert case_similarity(a, b) < 0.85
    assert case_similarity(a, dict(a)) == 1.0


# ---------- 测试点后处理 ----------


def test_validate_drops_points_without_valid_requirement_refs():
    points = [
        _point("TP-001", "验证码登录成功"),
        _point("TP-002", "引用不存在的需求", requirements=["REQ-999"]),
        _point("TP-003", "空引用", requirements=[]),
    ]
    accepted, dropped, issues = validate_test_points(points, {"REQ-001", "REQ-002"})

    assert [p["id"] for p in accepted] == ["TP-001"]
    assert [d["id"] for d in dropped] == ["TP-002", "TP-003"]
    assert "未关联到任何存在的需求点" in dropped[0]["reason"]
    assert any("没有任何测试点" in issue for issue in issues)


def test_validate_dedupes_similar_titles_sharing_requirement():
    points = [
        _point("TP-001", "验证码登录成功场景"),
        _point("TP-002", "验证码登录成功场景"),
        _point("TP-003", "验证码登录成功场景", requirements=["REQ-002"]),
    ]
    accepted, dropped, _ = validate_test_points(points, {"REQ-001", "REQ-002"})

    # 同需求点内重复被丢弃；跨需求点的同名测试点保留（可能是不同业务分支）
    assert [p["id"] for p in accepted] == ["TP-001", "TP-003"]
    assert [d["id"] for d in dropped] == ["TP-002"]
    assert "语义重复" in dropped[0]["reason"]


def test_validate_trims_overflow_by_priority_then_risk():
    points = []
    for index in range(1, 4):
        points.append(_point(f"TP-00{index}", f"低优先场景{index}", priority="P2"))
    for index in range(4, 9):
        points.append(
            _point(f"TP-00{index}", f"高优先场景{index}", priority="P0", risk_level="high")
        )
    accepted, dropped, issues = validate_test_points(points, {"REQ-001"})

    # 单需求点上限 = max(6, 3) = 6：保留 5 个 P0 + 1 个 P2
    assert len(accepted) == 6
    assert [p["id"] for p in accepted[:5]] == ["TP-004", "TP-005", "TP-006", "TP-007", "TP-008"]
    assert sorted(d["id"] for d in dropped) == ["TP-002", "TP-003"]
    assert all("超出测试点上限" in d["reason"] for d in dropped)
    assert any("超过上限" in issue for issue in issues)


def test_find_duplicate_test_points_reports_pairs():
    points = [_point("TP-001", "验证码登录成功场景"), _point("TP-002", "验证码登录成功场景")]
    pairs = find_duplicate_test_points(points)

    assert len(pairs) == 1
    assert {pairs[0]["case_a"], pairs[0]["case_b"]} == {"TP-001", "TP-002"}


# ---------- 技术选型 ----------


def test_detect_techniques_by_dimension():
    assert detect_techniques(_point("TP-001", "输入边界", dimension="boundary")) == [
        "boundary_value",
        "equivalence_partitioning",
    ]
    assert detect_techniques(_point("TP-001", "状态流转", dimension="state")) == ["state_transition"]
    assert detect_techniques(_point("TP-001", "权限校验", dimension="permission")) == ["scenario"]
    assert detect_techniques(_point("TP-001", "业务规则组合", dimension="business_rule")) == [
        "decision_table"
    ]
    assert detect_techniques(_point("TP-001", "异常容错", dimension="exception")) == ["error_guessing"]
    assert detect_techniques(_point("TP-001", "下单流程", dimension="flow")) == ["scenario"]
    assert detect_techniques(_point("TP-001", "功能验证", dimension="function")) == [
        "equivalence_partitioning"
    ]


def test_detect_techniques_uses_text_clues():
    # 维度是 function，但标题里出现"锁定"→ 状态迁移；出现"连续5次"→ 叠加边界值
    techniques = detect_techniques(_point("TP-001", "连续5次错误后锁定账号"))
    assert "state_transition" in techniques
    assert "boundary_value" in techniques


def test_detect_techniques_nonfunctional_is_exploratory_only():
    techniques = detect_techniques(_point("TP-001", "登录响应时间不超过2秒", dimension="nonfunctional"))
    assert techniques == ["exploratory"]


def test_detect_techniques_dedupes_and_preserves_order():
    techniques = detect_techniques(_point("TP-001", "状态锁定与解锁", dimension="boundary"))
    assert len(techniques) == len(set(techniques))


# ---------- 覆盖项 ----------


def test_build_coverage_items_uses_stable_ids_and_describes_data():
    point = _point("TP-001", "验证码登录", dimension="boundary")
    items = build_coverage_items(point, ["boundary_value", "equivalence_partitioning"])

    assert [item["id"] for item in items] == [
        "CI-TP001-01",
        "CI-TP001-02",
        "CI-TP001-03",
        "CI-TP001-04",
    ]
    assert all(item["test_point_id"] == "TP-001" for item in items)
    assert all(item["technique"] in ("boundary_value", "equivalence_partitioning") for item in items)
    # 覆盖项只描述"要取什么数据"，不含具体取值
    assert all(item["data_hint"] for item in items)
    assert all("验证码登录" in item["description"] for item in items)


def test_build_coverage_items_empty_for_unknown_technique():
    assert build_coverage_items(_point("TP-001", "x"), ["unknown_technique"]) == []


# ---------- 预算与设计策略 ----------


def test_allocate_case_budget_guarantees_one_and_prefers_p0():
    points = [
        _point("TP-001", "普通场景", priority="P1"),
        _point("TP-002", "高风险场景", priority="P0", risk_level="high"),
        _point("TP-003", "低优先场景", priority="P2"),
    ]
    budget = allocate_case_budget(points, 5)

    assert sum(budget.values()) == 5
    assert all(value >= 1 for value in budget.values())
    assert budget["TP-002"] == 2
    assert budget["TP-001"] == 2
    assert budget["TP-003"] == 1


def test_allocate_case_budget_caps_single_point():
    budget = allocate_case_budget([_point("TP-001", "唯一测试点")], 10)

    assert budget == {"TP-001": CASE_BATCH_SIZE}


def test_allocate_case_budget_keeps_every_point_when_budget_is_tight():
    points = [_point(f"TP-{index:03d}", f"场景{index}") for index in range(1, 9)]
    budget = allocate_case_budget(points, 3)

    assert len(budget) == 8
    assert all(value == 1 for value in budget.values())


def test_build_test_design_plan_covers_all_points():
    points = [
        _point("TP-001", "连续5次错误锁定", dimension="state", priority="P0", risk_level="high"),
        _point("TP-002", "权限校验", dimension="permission"),
    ]
    plan = build_test_design_plan(points)

    assert [item["test_point_id"] for item in plan["items"]] == ["TP-001", "TP-002"]
    assert plan["total_budget"] >= len(points)
    assert all(item["coverage_items"] for item in plan["items"])
    assert all(item["case_budget"] >= 1 for item in plan["items"])
    assert "state_transition" in plan["technique_summary"]
    assert plan["technique_summary"]["state_transition"] == 1


def test_build_test_design_plan_notes_tight_budget():
    points = [_point(f"TP-{index:03d}", f"场景{index}") for index in range(1, 13)]
    plan = build_test_design_plan(points, total_budget=5)

    assert any("超过用例预算" in note for note in plan["notes"])


def test_build_test_design_plan_handles_empty_input():
    plan = build_test_design_plan([])

    assert plan["items"] == []
    assert plan["total_budget"] == 0


# ---------- 编号归一与质检 ----------


def test_normalize_case_ids_is_unique_and_derived_from_test_point():
    cases = [
        {"id": "LLM-随便编的", "title": "A", "test_point_refs": ["TP-001"]},
        {"id": "TC-AUTH-999", "title": "B", "test_point_refs": ["TP-001"]},
        {"id": "x", "title": "C", "test_point_refs": ["TP-002"]},
        {"id": "y", "title": "D"},
    ]
    normalized = normalize_case_ids(cases)

    assert [case["id"] for case in normalized] == [
        "TC-TP001-01",
        "TC-TP001-02",
        "TC-TP002-01",
        "TC-GEN-01",
    ]
    assert len({case["id"] for case in normalized}) == 4


def test_case_quality_issues_flags_missing_fields():
    cases = [
        {"id": "TC-001", "title": "", "steps": [], "expected_results": [], "test_point_refs": []},
        {"id": "TC-002", "title": "正常", "steps": ["s"], "expected_results": ["e"], "test_point_refs": ["TP-001"]},
    ]
    issues = case_quality_issues(cases)

    assert any("缺少测试标题" in issue for issue in issues)
    assert any("缺少操作步骤" in issue for issue in issues)
    assert any("缺少预期结果" in issue for issue in issues)
    assert any("未关联任何测试点" in issue for issue in issues)
    assert not any("TC-002" in issue for issue in issues)
