"""工作流 API 端到端测试：认证、创建、SSE 运行、人工确认、重新生成、产物与导出"""

import json

import pytest

from conftest import parse_sse_events
from models.user import User


def _get_alice_id(db_session) -> int:
    return db_session.query(User).filter(User.username == "alice").first().id


def _create_workflow(client, name="登录功能测试", requirement="需求：新增手机号验证码登录，连续5次验证码错误后锁定30分钟"):
    response = client.post(
        "/api/workflows",
        json={"name": name, "requirement_text": requirement, "knowledge_base_id": None},
    )
    assert response.status_code == 200
    return response.json()


def test_create_workflow_requires_login(client):
    response = client.post(
        "/api/workflows",
        json={"name": "x", "requirement_text": "需求内容"},
    )
    assert response.status_code == 401


def test_list_workflows_requires_login(client):
    response = client.get("/api/workflows")
    assert response.status_code == 401


def test_get_other_users_workflow_returns_404(logged_in_client, make_user, make_workflow):
    bob = make_user("bob", "secret123")
    workflow = make_workflow(bob.id)
    response = logged_in_client.get(f"/api/workflows/{workflow.id}")
    assert response.status_code == 404


def test_create_workflow_requires_requirement_text(logged_in_client):
    response = logged_in_client.post(
        "/api/workflows",
        json={"name": "x", "requirement_text": "   "},
    )
    assert response.status_code == 400


def test_full_workflow_happy_path(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)

    # 启动：跑到人工确认 interrupt
    start_response = logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
    assert start_response.status_code == 200
    events = parse_sse_events(start_response.text)
    node_events = [e for e in events if e.get("event") == "node_done"]
    analysis_events = [e for e in node_events if e.get("node") == "requirement_analysis_agent"]
    assert analysis_events and analysis_events[0].get("artifact", {}).get("artifact_type") == "requirement_analysis"
    assert any(e.get("event") == "waiting_review" for e in events)

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "waiting_review"
    analysis_artifacts = [a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"]
    assert len(analysis_artifacts) == 1
    assert analysis_artifacts[0]["version"] == 1
    assert analysis_artifacts[0]["content"]["summary"] == stub_workflow_llm.analysis.summary

    # 确认（原样）：继续生成 + 覆盖检查
    approve_response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/approve",
        json={"analysis": None},
    )
    assert approve_response.status_code == 200
    approve_events = parse_sse_events(approve_response.text)
    assert any(e.get("event") == "completed" for e in approve_events)

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "completed"
    artifact_types = {a["artifact_type"] for a in detail["artifacts"]}
    assert artifact_types == {"requirement_analysis", "test_case_set", "coverage_report"}

    coverage = next(a for a in detail["artifacts"] if a["artifact_type"] == "coverage_report")
    assert coverage["content"]["total_cases"] == 2
    assert coverage["content"]["uncovered_requirements"] == []
    assert coverage["content"]["invalid_refs"] == []

    # 导出 CSV
    export_response = logged_in_client.get(f"/api/workflows/{workflow['id']}/export")
    assert export_response.status_code == 200
    assert "text/csv" in export_response.headers["content-type"]
    body = export_response.text
    assert "用例编号" in body
    assert "TC-AUTH-001" in body
    assert "TC-AUTH-002" in body


def test_approve_with_edited_analysis_creates_new_version(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    logged_in_client.post(f"/api/workflows/{workflow['id']}/start")

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    v1 = next(a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis")

    edited = dict(v1["content"])
    edited["summary"] = "人工修订后的需求概述"

    approve_response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/approve",
        json={"analysis": edited},
    )
    assert approve_response.status_code == 200
    assert any(e.get("event") == "completed" for e in parse_sse_events(approve_response.text))

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    versions = sorted(
        (a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"),
        key=lambda a: a["version"],
    )
    assert len(versions) == 2
    assert versions[1]["version"] == 2
    assert versions[1]["parent_artifact_id"] == v1["id"]
    assert versions[1]["content"]["summary"] == "人工修订后的需求概述"


def test_approve_invalid_analysis_returns_422(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    logged_in_client.post(f"/api/workflows/{workflow['id']}/start")

    # summary 是唯一必填字段，缺失即校验失败
    response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/approve",
        json={"analysis": {"scope": ["测试范围"]}},
    )
    assert response.status_code == 422

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "waiting_review"


def test_approve_wrong_status_returns_409(logged_in_client, db_session, make_workflow):
    workflow = make_workflow(_get_alice_id(db_session))
    response = logged_in_client.post(
        f"/api/workflows/{workflow.id}/approve",
        json={"analysis": None},
    )
    assert response.status_code == 409


def test_export_without_cases_returns_404(logged_in_client, db_session, make_workflow):
    workflow = make_workflow(_get_alice_id(db_session))
    response = logged_in_client.get(f"/api/workflows/{workflow.id}/export")
    assert response.status_code == 404


def test_create_workflow_with_unknown_kb_returns_404(logged_in_client):
    response = logged_in_client.post(
        "/api/workflows",
        json={"name": "x", "requirement_text": "需求", "knowledge_base_id": "no-such-kb"},
    )
    assert response.status_code == 404


def test_regenerate_after_completion_creates_new_versions(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
    logged_in_client.post(
        f"/api/workflows/{workflow['id']}/approve", json={"analysis": None}
    )
    assert stub_workflow_llm.calls == 2  # 分析 1 次 + 生成 1 次

    regenerate_response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/regenerate"
    )
    assert regenerate_response.status_code == 200
    assert any(
        e.get("event") == "completed" for e in parse_sse_events(regenerate_response.text)
    )
    assert stub_workflow_llm.calls == 3  # 只重跑了生成

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "completed"
    case_versions = [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "test_case_set"]
    assert case_versions == [1, 2]
    coverage_versions = [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "coverage_report"]
    assert coverage_versions == [1, 2]
    # 需求分析保持原版本，未被重新生成
    analysis_versions = [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"]
    assert analysis_versions == [1]


def test_regenerate_wrong_status_returns_409(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)  # created 状态，尚未运行
    response = logged_in_client.post(f"/api/workflows/{workflow['id']}/regenerate")
    assert response.status_code == 409


def test_regenerate_with_edited_analysis_saves_new_version(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
    logged_in_client.post(
        f"/api/workflows/{workflow['id']}/approve", json={"analysis": None}
    )

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    v1 = next(a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis")
    edited = dict(v1["content"])
    edited["summary"] = "重生成前的人工修订概述"

    response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/regenerate",
        json={"analysis": edited},
    )
    assert response.status_code == 200
    assert any(e.get("event") == "completed" for e in parse_sse_events(response.text))

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    analysis_versions = sorted(
        (a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"),
        key=lambda a: a["version"],
    )
    assert [a["version"] for a in analysis_versions] == [1, 2]
    assert analysis_versions[1]["parent_artifact_id"] == v1["id"]
    assert analysis_versions[1]["content"]["summary"] == "重生成前的人工修订概述"
    case_versions = [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "test_case_set"]
    assert case_versions == [1, 2]
    assert detail["status"] == "completed"


def test_regenerate_invalid_analysis_returns_422(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
    logged_in_client.post(
        f"/api/workflows/{workflow['id']}/approve", json={"analysis": None}
    )

    response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/regenerate",
        json={"analysis": {"scope": ["缺少 summary"]}},
    )
    assert response.status_code == 422
    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "completed"


def test_event_artifact_extraction():
    from api.endpoints.workflow_api import _event_artifact

    assert _event_artifact("load_requirement", {"requirement_text": "x"}) is None
    assert _event_artifact("persist_failure", {"error": "e"}) is None

    retrieved = _event_artifact(
        "retrieve_knowledge",
        {"retrieved_documents": [{"source": "《a.pdf》 第1页", "content": "c"}]},
    )
    assert retrieved["artifact_type"] == "retrieved_context"
    assert retrieved["content"]["documents"][0]["source"] == "《a.pdf》 第1页"

    analysis = _event_artifact(
        "requirement_analysis_agent", {"requirement_analysis": {"summary": "s"}}
    )
    assert analysis["artifact_type"] == "requirement_analysis"

    cases = _event_artifact(
        "test_case_generation_agent", {"test_cases": [{"id": "TC-A-001"}]}
    )
    assert cases["artifact_type"] == "test_case_set"

    report = _event_artifact("coverage_check", {"coverage_report": {"total_cases": 1}})
    assert report["artifact_type"] == "coverage_report"


@pytest.mark.asyncio
async def test_retrieve_knowledge_saves_context_artifact(
    test_env, db_session, make_user, make_workflow, monkeypatch
):
    import workflows.nodes as workflow_nodes
    from langchain_core.documents import Document
    from services import workflow_service

    class FakeRetriever:
        async def get_relevant_documents(self, query):
            return [
                Document(page_content="验证码有效期为5分钟", metadata={"filename": "登录需求.pdf", "page": 3}),
                Document(page_content="连续5次错误后锁定30分钟", metadata={"filename": "登录需求.pdf", "page": 4}),
            ]

    async def fake_get_retriever(kb_id, db, user_id):
        return FakeRetriever()

    monkeypatch.setattr(workflow_nodes, "get_rag_retriever_by_kb", fake_get_retriever)

    user = make_user("wfnodes", "secret123")
    workflow = make_workflow(user.id, knowledge_base_id="kb-1")
    state = {
        "workflow_id": workflow.id,
        "user_id": workflow.user_id,
        "knowledge_base_id": "kb-1",
        "requirement_text": "手机号验证码登录",
    }
    result = await workflow_nodes.retrieve_knowledge(state)

    assert len(result["retrieved_documents"]) == 2
    artifact = workflow_service.get_latest_artifact(workflow.id, "retrieved_context")
    assert artifact is not None
    content = json.loads(artifact.content)
    assert content["knowledge_base_id"] == "kb-1"
    assert content["documents"][0]["source"] == "《登录需求.pdf》 第3页"


def test_truncated_case_output_is_flagged_in_artifact(logged_in_client, stub_workflow_llm):
    """输出被截断并抢救出部分用例时，test_case_set 产物必须带 truncated 标记"""
    stub_workflow_llm.truncated = True
    workflow = _create_workflow(logged_in_client)

    logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
    logged_in_client.post(
        f"/api/workflows/{workflow['id']}/approve",
        json={"analysis": None},
    )

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    cases_artifact = next(a for a in detail["artifacts"] if a["artifact_type"] == "test_case_set")
    assert cases_artifact["content"]["truncated"] is True
    assert len(cases_artifact["content"]["test_cases"]) == 2


def test_event_artifact_carries_truncated_flag():
    from api.endpoints.workflow_api import _event_artifact

    artifact = _event_artifact(
        "test_case_generation_agent",
        {"test_cases": [{"id": "TC-1"}], "cases_truncated": True},
    )
    assert artifact["content"]["truncated"] is True

    artifact = _event_artifact(
        "test_case_generation_agent",
        {"test_cases": [{"id": "TC-1"}], "cases_truncated": False},
    )
    assert artifact["content"]["truncated"] is False


def test_try_claim_workflow_optimistic_lock(db_session, make_user, make_workflow):
    """状态机乐观锁：同一抢占条件只有第一次成功"""
    from services import workflow_service

    user = make_user("claim_user", "secret123")
    wf = make_workflow(user.id)

    assert workflow_service.try_claim_workflow(
        wf.id, ("created", "failed"), status="analyzing", current_step="load_requirement"
    ) is True
    # 状态已迁移出 expected_statuses，再次抢占失败
    assert workflow_service.try_claim_workflow(
        wf.id, ("created", "failed"), status="analyzing", current_step="load_requirement"
    ) is False


@pytest.mark.asyncio
async def test_start_run_dedupes_active_workflow(monkeypatch):
    """同一任务的后台运行在进程内只有一个实例"""
    import asyncio

    from api.endpoints import workflow_api

    async def fake_runner(workflow_id, run_input, config_extra=None):
        await asyncio.Event().wait()  # 模拟长时间运行

    monkeypatch.setattr(workflow_api, "_run_workflow_graph", fake_runner)

    run1 = workflow_api._start_run("wf-dedupe", {"workflow_id": "wf-dedupe"})
    run2 = workflow_api._start_run("wf-dedupe", None)
    assert run1 is run2

    run1.task.cancel()
    try:
        await run1.task
    except asyncio.CancelledError:
        pass
    workflow_api._active_runs.pop("wf-dedupe", None)


@pytest.mark.asyncio
async def test_background_run_publishes_and_subscriber_replays(monkeypatch):
    """后台运行发布事件；订阅者（含晚接入的）能重放并收到结束标记"""
    import asyncio

    from api.endpoints import workflow_api

    async def fake_runner(workflow_id, run_input, config_extra=None):
        run = workflow_api._active_runs[workflow_id]
        workflow_api._publish_event(run, {"event": "node_done", "node": "load_requirement"})
        workflow_api._publish_event(run, {"event": "completed"})
        # 模拟真实 runner 的 finally：发结束哨兵并清理注册表
        run.done = True
        for q in run.subscribers:
            q.put_nowait(None)
        workflow_api._active_runs.pop(workflow_id, None)

    monkeypatch.setattr(workflow_api, "_run_workflow_graph", fake_runner)

    run = workflow_api._start_run("wf-sub", {"workflow_id": "wf-sub"})
    queue = workflow_api._subscribe(run)
    events = []
    async for chunk in workflow_api._forward_workflow_events(run, queue):
        for line in chunk.split("\n\n"):
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                events.append(line)

    assert any("node_done" in e for e in events)
    assert any("completed" in e for e in events)
    # 运行结束后清理注册表
    await run.task
    assert "wf-sub" not in workflow_api._active_runs
