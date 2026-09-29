"""工作流失败路径测试：结构化输出失败重试与 failed 状态落库"""

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
