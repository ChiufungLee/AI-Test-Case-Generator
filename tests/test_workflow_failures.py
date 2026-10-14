"""工作流失败路径测试：结构化输出失败重试与 failed 状态落库"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from schemas.workflow_schemas import TestPointSet as PointSetModel
from workflows import nodes as workflow_nodes

from conftest import parse_sse_events


def _create_and_start(client, payload=None):
    response = client.post(
        "/api/workflows",
        json=payload
        or {"name": "失败路径测试", "requirement_text": "需求：手机号验证码登录", "knowledge_base_id": None},
    )
    assert response.status_code == 200
    workflow_id = response.json()["id"]
    start_response = client.post(f"/api/workflows/{workflow_id}/start")
    assert start_response.status_code == 200
    return workflow_id, parse_sse_events(start_response.text)


def test_structured_output_failure_marks_workflow_failed(logged_in_client, stub_workflow_llm):
    stub_workflow_llm.fail_times = 999  # 所有尝试都失败

    workflow_id, events = _create_and_start(logged_in_client)

    assert any(e.get("event") == "failed" for e in events)
    failed_event = next(e for e in events if e.get("event") == "failed")
    assert "需求分析失败" in failed_event["error"]
    # 重试机制：单节点共尝试 2 次结构化调用
    assert stub_workflow_llm.calls == 2

    detail = logged_in_client.get(f"/api/workflows/{workflow_id}").json()
    assert detail["status"] == "failed"
    assert "需求分析失败" in detail["error"]
    assert detail["artifacts"] == []


def test_structured_output_retry_succeeds_on_second_attempt(logged_in_client, stub_workflow_llm):
    stub_workflow_llm.fail_times = 1  # 第一次失败，重试成功

    workflow_id, events = _create_and_start(logged_in_client)

    assert any(e.get("event") == "waiting_review" for e in events)
    assert stub_workflow_llm.calls == 2

    detail = logged_in_client.get(f"/api/workflows/{workflow_id}").json()
    assert detail["status"] == "waiting_review"
    assert detail["error"] is None
    analysis_artifacts = [a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"]
    assert len(analysis_artifacts) == 1


def test_failed_workflow_can_be_rerun(logged_in_client, stub_workflow_llm):
    stub_workflow_llm.fail_times = 999
    workflow_id, _ = _create_and_start(logged_in_client)

    # 第一次运行失败
    detail = logged_in_client.get(f"/api/workflows/{workflow_id}").json()
    assert detail["status"] == "failed"

    # 恢复桩为正常后重跑，应能走到人工确认
    stub_workflow_llm.fail_times = 0
    stub_workflow_llm.calls = 0
    rerun_response = logged_in_client.post(f"/api/workflows/{workflow_id}/start")
    assert rerun_response.status_code == 200
    assert any(e.get("event") == "waiting_review" for e in parse_sse_events(rerun_response.text))

    detail = logged_in_client.get(f"/api/workflows/{workflow_id}").json()
    assert detail["status"] == "waiting_review"
    assert detail["error"] is None


# ---------- 兜底路径返回空结果（D-044 线上事故） ----------


class _NoneStructuredOutput:
    """模拟 with_structured_output 在「模型没有 tool_calls」时的行为：返回 None 而不抛错"""

    def __init__(self, calls: list):
        self.calls = calls

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(1)
        return None


class _PlainTextModel:
    """模型只回正文（不是 JSON、也不走工具调用）；bind/with_structured_output 与 ChatModel 同形"""

    def __init__(self, text: str = "本轮覆盖了所有功能需求点，不测性能与兼容性。"):
        self.text = text
        self.structured_calls: list = []

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=self.text)

    def with_structured_output(self, schema, **kwargs):
        return _NoneStructuredOutput(self.structured_calls)


@pytest.mark.asyncio
async def test_invoke_structured_raises_when_fallback_returns_none(test_env, monkeypatch):
    """兜底返回 None 必须转成异常：否则重试循环把它当成功，调用方 AttributeError 崩整轮"""
    model = _PlainTextModel()
    monkeypatch.setattr(workflow_nodes, "get_llm_model", lambda: model)

    with pytest.raises(ValueError, match="未返回结构化结果"):
        await workflow_nodes._invoke_structured(
            [HumanMessage(content="设计测试点")], PointSetModel, 0.3
        )

    assert len(model.structured_calls) == 1


@pytest.mark.asyncio
async def test_test_point_design_reports_error_instead_of_crashing(
    test_env, db_session, make_user, make_workflow, monkeypatch
):
    """回归（线上事故 D-044）：空结果走「测试点设计失败」的失败分支，而不是 result.test_points 崩掉"""
    user = make_user("empty_fallback_user", "secret123")
    workflow = make_workflow(user.id)
    model = _PlainTextModel()
    monkeypatch.setattr(workflow_nodes, "get_llm_model", lambda: model)

    result = await workflow_nodes.test_point_design_agent(
        {
            "workflow_id": workflow.id,
            "requirement_analysis": {
                "summary": "登录需求",
                "functional_requirements": [
                    {"id": "REQ-001", "title": "验证码登录", "description": ""}
                ],
            },
            "retrieved_documents": [],
        }
    )

    assert "error" in result
    assert "测试点设计失败" in result["error"]
    # 节点级重试 2 次：纯文本解析失败 + 兜底空结果都算失败
    assert len(model.structured_calls) == 2
