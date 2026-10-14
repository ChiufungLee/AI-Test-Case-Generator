"""测试点设计（阶段 2）、设计策略（阶段 3）与覆盖校验（阶段 5）的节点级测试（D-036）"""

import json

import pytest

from schemas.workflow_schemas import TestPoint as PointModel
from schemas.workflow_schemas import TestPointSet as PointSetModel
from services import workflow_service
from workflows import nodes as workflow_nodes


def _analysis() -> dict:
    return {
        "summary": "登录需求",
        "functional_requirements": [
            {"id": "REQ-001", "title": "验证码登录", "description": ""},
            {"id": "REQ-002", "title": "错误锁定", "description": ""},
        ],
    }


def test_numeric_ids_from_llm_are_coerced_to_text():
    """回归：LLM 把批内临时序号输出成数字（1/2/3）时不得让整批校验失败

    真实事故——改成「按测试点分批、批内临时序号」后，提示词里的 id 从 TC-XXX 变成
    1、2、3，模型输出 {"id": 1} 触发 TestCaseSet 的 string_type 校验错误，
    整批退化到 with_structured_output 兜底（再失败即整批丢失）。
    """
    from schemas.workflow_schemas import TestCaseSet

    parsed = TestCaseSet.model_validate_json(
        '{"test_cases": [{"id": 1, "title": "用例一"}, {"id": 2, "title": "用例二"}]}'
    )

    assert [case.id for case in parsed.test_cases] == ["1", "2"]


def test_test_point_numeric_id_is_coerced_to_text():
    from schemas.workflow_schemas import TestPointSet

    parsed = TestPointSet.model_validate_json(
        '{"test_points": [{"id": 1, "title": "登录成功", "requirement_refs": ["REQ-001"]}]}'
    )

    assert parsed.test_points[0].id == "1"


def test_missing_case_id_is_still_rejected():
    """类型收敛不放宽必填约束：资产层（D-015）把 case_id 当不可变业务身份，空编号必须拒绝"""
    from schemas.workflow_schemas import TestCaseSet

    with pytest.raises(Exception):
        TestCaseSet.model_validate_json('{"test_cases": [{"title": "无编号用例"}]}')


def test_string_notes_are_coerced_to_list():
    """回归（线上事故 D-044）：notes 被写成一段话时不得让整批校验失败

    真实事故——测试点设计返回的 notes 是字符串（"本轮覆盖了所有功……短信地区限制。"），
    list_type 校验失败后退化到 with_structured_output 兜底，而这条路径在模型不肯走
    工具调用时会返回 None，最终以 AttributeError 崩掉整轮运行。
    """
    parsed = PointSetModel.model_validate_json(
        '{"test_points": [], "coverage_target": "覆盖登录", "notes": "  本轮不测性能与兼容性。  "}'
    )

    assert parsed.notes == ["本轮不测性能与兼容性。"]
    # 空串归位成空数组；已经是数组的原样保留（不按换行拆分）
    assert PointSetModel.model_validate({"notes": "   "}).notes == []
    assert PointSetModel.model_validate({"notes": ["一", "二"]}).notes == ["一", "二"]


def test_string_arrays_in_analysis_and_cases_are_coerced():
    """同一类错配的其它字段一并收敛：需求分析的各清单、用例的步骤/预期/追溯编号"""
    from schemas.workflow_schemas import RequirementAnalysis, TestCase, TestCaseSet

    analysis = RequirementAnalysis.model_validate(
        {"summary": "登录", "scope": "登录与锁定", "roles": "普通用户"}
    )
    assert analysis.scope == ["登录与锁定"]
    assert analysis.roles == ["普通用户"]

    case = TestCase.model_validate(
        {
            "id": "1",
            "title": "登录成功",
            "steps": "1. 打开登录页\n2. 输入验证码",
            "expected_results": "登录成功并跳转首页",
            "requirement_refs": "REQ-001",
        }
    )
    assert case.steps == ["1. 打开登录页\n2. 输入验证码"]
    assert case.expected_results == ["登录成功并跳转首页"]
    assert case.requirement_refs == ["REQ-001"]
    # 必填约束不受影响：id 仍不可缺
    assert TestCaseSet.model_validate({"test_cases": [{"id": 7, "title": "x"}]}).test_cases[0].id == "7"


@pytest.mark.asyncio
async def test_test_point_design_drops_invalid_and_duplicate_points(
    test_env, db_session, make_user, make_workflow, stub_workflow_llm
):
    """引用不存在的需求点与语义重复的测试点由确定性规则丢弃，并随产物落库"""
    user = make_user("tp_user", "secret123")
    workflow = make_workflow(user.id)
    stub_workflow_llm.test_point_sets_sequence = [
        PointSetModel(
            test_points=[
                PointModel(
                    id="TP-001",
                    title="验证码登录成功",
                    requirement_refs=["REQ-001"],
                    priority="P0",
                ),
                PointModel(id="TP-002", title="验证码登录成功", requirement_refs=["REQ-001"]),
                PointModel(id="TP-003", title="引用不存在的需求", requirement_refs=["REQ-999"]),
                PointModel(id="TP-004", title="错误次数锁定", requirement_refs=["REQ-002"]),
            ],
            coverage_target="覆盖登录与锁定",
        )
    ]

    result = await workflow_nodes.test_point_design_agent(
        {"workflow_id": workflow.id, "user_id": user.id, "requirement_analysis": _analysis()}
    )

    content = result["test_point_set"]
    assert [point["id"] for point in content["test_points"]] == ["TP-001", "TP-004"]
    assert len(content["dropped_test_points"]) == 2
    assert any("未关联到任何存在的需求点" in d["reason"] for d in content["dropped_test_points"])
    assert any("语义重复" in d["reason"] for d in content["dropped_test_points"])
    assert content["coverage_target"] == "覆盖登录与锁定"

    artifact = workflow_service.get_latest_artifact(workflow.id, "test_point_set")
    assert artifact is not None
    assert len(json.loads(artifact.content)["test_points"]) == 2


@pytest.mark.asyncio
async def test_test_point_design_fails_when_no_valid_point(
    test_env, db_session, make_user, make_workflow, stub_workflow_llm
):
    """全部测试点都未关联有效需求点时节点报错，不产出空产物"""
    user = make_user("tp_user2", "secret123")
    workflow = make_workflow(user.id)
    stub_workflow_llm.test_point_sets_sequence = [
        PointSetModel(test_points=[PointModel(id="TP-001", title="无引用", requirement_refs=["REQ-999"])])
    ]

    result = await workflow_nodes.test_point_design_agent(
        {"workflow_id": workflow.id, "user_id": user.id, "requirement_analysis": _analysis()}
    )

    assert "error" in result
    assert workflow_service.get_latest_artifact(workflow.id, "test_point_set") is None


@pytest.mark.asyncio
async def test_test_design_plan_node_saves_deterministic_plan(
    test_env, db_session, make_user, make_workflow
):
    """设计策略节点零 LLM：技术、覆盖项与预算全部由确定性代码产出并落库"""
    user = make_user("plan_user", "secret123")
    workflow = make_workflow(user.id)
    test_point_set = {
        "test_points": [
            {
                "id": "TP-001",
                "title": "连续5次错误后锁定",
                "description": "错误次数达到上限的锁定行为",
                "requirement_refs": ["REQ-002"],
                "dimension": "boundary",
                "risk_level": "high",
                "priority": "P0",
                "rationale": "",
            }
        ],
        "coverage_target": "",
        "notes": [],
    }

    result = await workflow_nodes.test_design_plan_node(
        {"workflow_id": workflow.id, "user_id": user.id, "test_point_set": test_point_set}
    )

    plan = result["test_design_plan"]
    assert [item["test_point_id"] for item in plan["items"]] == ["TP-001"]
    assert plan["items"][0]["case_budget"] >= 1
    assert plan["items"][0]["coverage_items"]
    assert "boundary_value" in plan["technique_summary"]

    artifact = workflow_service.get_latest_artifact(workflow.id, "test_design_plan")
    assert artifact is not None


@pytest.mark.asyncio
async def test_coverage_check_reports_test_point_coverage(
    test_env, db_session, make_user, make_workflow
):
    """覆盖校验按测试点/覆盖项清单核算，并给出未覆盖测试点与质量问题"""
    user = make_user("cov_user", "secret123")
    workflow = make_workflow(user.id)
    state = {
        "workflow_id": workflow.id,
        "user_id": user.id,
        "requirement_analysis": _analysis(),
        "test_point_set": {
            "test_points": [
                {
                    "id": "TP-001",
                    "title": "登录成功",
                    "requirement_refs": ["REQ-001"],
                    "dimension": "function",
                },
                {
                    "id": "TP-002",
                    "title": "锁定行为",
                    "requirement_refs": ["REQ-002"],
                    "dimension": "boundary",
                },
            ]
        },
        "test_design_plan": {
            "items": [
                {"test_point_id": "TP-001", "coverage_items": [{"id": "CI-TP001-01"}]},
                {"test_point_id": "TP-002", "coverage_items": [{"id": "CI-TP002-01"}]},
            ]
        },
        "test_cases": [
            {
                "id": "TC-TP001-01",
                "title": "登录成功",
                "steps": ["输入验证码", "提交"],
                "expected_results": ["登录成功"],
                "priority": "P0",
                "requirement_refs": ["REQ-001"],
                "test_point_refs": ["TP-001"],
                "covered_items": ["CI-TP001-01"],
            }
        ],
        "failed_test_points": [],
    }

    result = await workflow_nodes.coverage_check(state)
    report = result["coverage_report"]

    assert report["total_test_points"] == 2
    assert report["covered_test_points"] == ["TP-001"]
    assert report["uncovered_test_points"] == ["TP-002"]
    assert report["uncovered_requirements"] == ["REQ-002"]
    assert report["uncovered_coverage_items"] == ["CI-TP002-01"]
    assert report["invalid_test_point_refs"] == []
    assert report["dimension_summary"] == {"boundary": 1, "function": 1}
    assert any("TP-002" in issue for issue in report["quality_issues"])

    updated = workflow_service.get_workflow(workflow.id)
    assert updated.status == "completed"
    artifact = workflow_service.get_latest_artifact(workflow.id, "coverage_report")
    assert artifact is not None


@pytest.mark.asyncio
async def test_generation_batches_failures_are_isolated(
    test_env, db_session, make_user, make_workflow, stub_workflow_llm
):
    """单个测试点的批次失败不影响其余测试点，失败编号记入产物"""
    user = make_user("batch_user", "secret123")
    workflow = make_workflow(user.id)
    state = {
        "workflow_id": workflow.id,
        "user_id": user.id,
        "requirement_analysis": _analysis(),
        "test_point_set": {
            "test_points": [
                {"id": "TP-001", "title": "登录成功", "requirement_refs": ["REQ-001"]},
                {"id": "TP-002", "title": "锁定行为", "requirement_refs": ["REQ-002"]},
            ]
        },
        "test_design_plan": {
            "items": [
                {"test_point_id": "TP-001", "coverage_items": [], "case_budget": 2},
                {"test_point_id": "TP-002", "coverage_items": [], "case_budget": 2},
            ]
        },
    }

    # 第一批（TP-001）耗尽重试后失败：fail_times=2 正好吃掉两次尝试
    stub_workflow_llm.fail_times = 2
    result = await workflow_nodes.test_case_generation_agent(state)

    assert result["failed_test_points"] == ["TP-001"]
    assert len(result["test_cases"]) == 2
    assert all(case["test_point_refs"] == ["TP-002"] for case in result["test_cases"])

    artifact = workflow_service.get_latest_artifact(workflow.id, "test_case_set")
    content = json.loads(artifact.content)
    assert content["failed_test_points"] == ["TP-001"]
