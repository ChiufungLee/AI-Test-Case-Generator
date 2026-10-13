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


def _start(client, workflow_id):
    response = client.post(f"/api/workflows/{workflow_id}/start")
    assert response.status_code == 200, response.text
    return parse_sse_events(response.text)


def _approve(client, workflow_id, **payload):
    """人工确认：两个确认点共用入口，载荷只带当前 gate 需要的键（空表示原样确认）"""
    response = client.post(f"/api/workflows/{workflow_id}/approve", json=payload)
    assert response.status_code == 200, response.text
    return parse_sse_events(response.text)


def _run_to_completion(client, workflow_id):
    """启动 → 需求分析确认 → 测试点确认（均为原样确认）→ 返回最后一次确认的事件"""
    _start(client, workflow_id)
    _approve(client, workflow_id)
    return _approve(client, workflow_id)


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

    # 启动：跑到需求分析确认 interrupt
    events = _start(logged_in_client, workflow["id"])
    node_events = [e for e in events if e.get("event") == "node_done"]
    analysis_events = [e for e in node_events if e.get("node") == "requirement_analysis_agent"]
    assert analysis_events and analysis_events[0].get("artifact", {}).get("artifact_type") == "requirement_analysis"
    assert any(e.get("event") == "waiting_review" for e in events)

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "waiting_review"
    assert detail["current_step"] == "human_review"
    analysis_artifacts = [a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"]
    assert len(analysis_artifacts) == 1
    assert analysis_artifacts[0]["version"] == 1
    assert analysis_artifacts[0]["content"]["summary"] == stub_workflow_llm.analysis.summary

    # 第一次确认（原样）：测试点设计 + 确定性设计策略 → 停在测试点确认
    approve_events = _approve(logged_in_client, workflow["id"])
    assert any(e.get("event") == "waiting_review" for e in approve_events)
    assert any(
        e.get("event") == "node_done" and e.get("node") == "test_point_design_agent"
        for e in approve_events
    )

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "waiting_review"
    assert detail["current_step"] == "human_review_design"
    point_artifact = next(a for a in detail["artifacts"] if a["artifact_type"] == "test_point_set")
    assert len(point_artifact["content"]["test_points"]) == 2
    plan_artifact = next(a for a in detail["artifacts"] if a["artifact_type"] == "test_design_plan")
    assert len(plan_artifact["content"]["items"]) == 2
    assert all(item["coverage_items"] for item in plan_artifact["content"]["items"])

    # 第二次确认（原样）：用例生成 + 覆盖校验 → 完成
    final_events = _approve(logged_in_client, workflow["id"])
    assert any(e.get("event") == "completed" for e in final_events)

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "completed"
    artifact_types = {a["artifact_type"] for a in detail["artifacts"]}
    assert artifact_types == {
        "requirement_analysis",
        "test_point_set",
        "test_design_plan",
        "test_case_set",
        "coverage_report",
    }

    coverage = next(a for a in detail["artifacts"] if a["artifact_type"] == "coverage_report")
    # 2 个测试点 × 桩每批返回的 2 条用例
    assert coverage["content"]["total_cases"] == 4
    assert coverage["content"]["total_test_points"] == 2
    assert coverage["content"]["uncovered_test_points"] == []
    assert coverage["content"]["uncovered_requirements"] == []
    assert coverage["content"]["invalid_refs"] == []
    assert coverage["content"]["invalid_test_point_refs"] == []

    # 导出 CSV：用例编号由服务端按测试点统一派生
    export_response = logged_in_client.get(f"/api/workflows/{workflow['id']}/export")
    assert export_response.status_code == 200
    assert "text/csv" in export_response.headers["content-type"]
    body = export_response.text
    assert "用例编号" in body
    assert "TC-TP001-01" in body
    assert "TC-TP002-01" in body


def test_approve_with_edited_analysis_creates_new_version(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    _start(logged_in_client, workflow["id"])

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    v1 = next(a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis")

    edited = dict(v1["content"])
    edited["summary"] = "人工修订后的需求概述"

    approve_events = _approve(logged_in_client, workflow["id"], analysis=edited)
    # 修订后继续跑到下一个确认点（测试点确认）
    assert any(e.get("event") == "waiting_review" for e in approve_events)

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    versions = sorted(
        (a for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"),
        key=lambda a: a["version"],
    )
    assert len(versions) == 2
    assert versions[1]["version"] == 2
    assert versions[1]["parent_artifact_id"] == v1["id"]
    assert versions[1]["content"]["summary"] == "人工修订后的需求概述"
    assert detail["current_step"] == "human_review_design"


def test_approve_invalid_analysis_returns_422(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    _start(logged_in_client, workflow["id"])

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


def test_export_works_for_failed_workflow_with_cases(logged_in_client, db_session, make_workflow):
    # 导出以产物为准、不看状态：跑失败但已生成出用例的任务同样能导出（D-040）
    from services import workflow_service

    workflow = make_workflow(_get_alice_id(db_session), status="failed")
    workflow_service.save_artifact(
        workflow.id,
        "test_case_set",
        {
            "test_cases": [
                {
                    "id": "TC-AUTH-001",
                    "title": "登录成功",
                    "preconditions": ["用户已注册"],
                    "steps": ["打开登录页", "输入验证码"],
                    "expected_results": ["进入首页"],
                    "priority": "P1",
                    "automation": "Manual",
                    "requirement_refs": ["REQ-001"],
                }
            ]
        },
    )

    response = logged_in_client.get(f"/api/workflows/{workflow.id}/export")

    assert response.status_code == 200
    assert "TC-AUTH-001" in response.text


def test_create_workflow_with_unknown_kb_returns_404(logged_in_client):
    response = logged_in_client.post(
        "/api/workflows",
        json={"name": "x", "requirement_text": "需求", "knowledge_base_id": "no-such-kb"},
    )
    assert response.status_code == 404


def test_regenerate_after_completion_creates_new_versions(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    _run_to_completion(logged_in_client, workflow["id"])
    calls_after_first_run = stub_workflow_llm.calls  # 分析 1 + 测试点 1 + 每测试点 1 次生成

    regenerate_response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/regenerate"
    )
    assert regenerate_response.status_code == 200
    assert any(
        e.get("event") == "completed" for e in parse_sse_events(regenerate_response.text)
    )
    # 只重跑了用例生成：每个测试点一次
    assert stub_workflow_llm.calls - calls_after_first_run == 2

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "completed"
    case_versions = [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "test_case_set"]
    assert case_versions == [1, 2]
    coverage_versions = [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "coverage_report"]
    assert coverage_versions == [1, 2]
    # 需求分析、测试点与设计策略保持原版本，未被重新生成
    assert [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "requirement_analysis"] == [1]
    assert [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "test_point_set"] == [1]
    assert [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "test_design_plan"] == [1]


def test_regenerate_test_points_reruns_design_and_stops_at_review(logged_in_client, stub_workflow_llm):
    """target=test_points：从需求分析确认之后重跑测试点设计与设计策略，重新停在测试点确认"""
    workflow = _create_workflow(logged_in_client)
    _run_to_completion(logged_in_client, workflow["id"])

    response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/regenerate",
        json={"target": "test_points"},
    )
    assert response.status_code == 200
    events = parse_sse_events(response.text)
    assert any(e.get("event") == "waiting_review" for e in events)
    assert any(
        e.get("event") == "node_done" and e.get("node") == "test_point_design_agent" for e in events
    )

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    assert detail["status"] == "waiting_review"
    assert detail["current_step"] == "human_review_design"
    assert [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "test_point_set"] == [1, 2]
    # 用例尚未重新生成：仍是上一轮的 1 个版本
    assert [a["version"] for a in detail["artifacts"] if a["artifact_type"] == "test_case_set"] == [1]


def test_regenerate_with_edited_test_points_recomputes_design_plan(logged_in_client, stub_workflow_llm):
    """人工修订测试点后重跑：追加测试点新版本，并由确定性代码重算设计策略"""
    workflow = _create_workflow(logged_in_client)
    _run_to_completion(logged_in_client, workflow["id"])

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    points_v1 = next(a for a in detail["artifacts"] if a["artifact_type"] == "test_point_set")
    plan_v1 = next(a for a in detail["artifacts"] if a["artifact_type"] == "test_design_plan")

    edited = {
        "test_points": [
            {
                "id": "TP-001",
                "title": "人工新增的边界测试点",
                "description": "验证码错误次数边界",
                "requirement_refs": ["REQ-002"],
                "dimension": "boundary",
                "risk_level": "high",
                "priority": "P0",
                "rationale": "人工补充",
            }
        ],
        "coverage_target": "人工修订后的覆盖目标",
        "notes": [],
    }

    response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/regenerate",
        json={"test_point_set": edited},
    )
    assert response.status_code == 200
    assert any(e.get("event") == "completed" for e in parse_sse_events(response.text))

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    points_versions = sorted(
        (a for a in detail["artifacts"] if a["artifact_type"] == "test_point_set"),
        key=lambda a: a["version"],
    )
    assert [a["version"] for a in points_versions] == [1, 2]
    assert points_versions[1]["parent_artifact_id"] == points_v1["id"]
    assert points_versions[1]["content"]["coverage_target"] == "人工修订后的覆盖目标"

    plan_versions = sorted(
        (a for a in detail["artifacts"] if a["artifact_type"] == "test_design_plan"),
        key=lambda a: a["version"],
    )
    # 设计策略随测试点重算：只保留人工修订后的那一个测试点
    assert [a["version"] for a in plan_versions] == [1, 2]
    assert plan_versions[1]["parent_artifact_id"] == plan_v1["id"]
    assert [item["test_point_id"] for item in plan_versions[1]["content"]["items"]] == ["TP-001"]
    assert "boundary_value" in plan_versions[1]["content"]["technique_summary"]


def test_regenerate_rejects_empty_test_points(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    _run_to_completion(logged_in_client, workflow["id"])

    response = logged_in_client.post(
        f"/api/workflows/{workflow['id']}/regenerate",
        json={"test_point_set": {"test_points": []}},
    )
    assert response.status_code == 422
    assert "测试点不能为空" in response.json()["error"]


def test_regenerate_wrong_status_returns_409(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)  # created 状态，尚未运行
    response = logged_in_client.post(f"/api/workflows/{workflow['id']}/regenerate")
    assert response.status_code == 409


def test_regenerate_with_edited_analysis_saves_new_version(logged_in_client, stub_workflow_llm):
    workflow = _create_workflow(logged_in_client)
    _run_to_completion(logged_in_client, workflow["id"])

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
    _run_to_completion(logged_in_client, workflow["id"])

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

    points = _event_artifact("test_point_design_agent", {"test_point_set": {"test_points": []}})
    assert points["artifact_type"] == "test_point_set"

    plan = _event_artifact("test_design_plan", {"test_design_plan": {"items": []}})
    assert plan["artifact_type"] == "test_design_plan"

    reviewed_points = _event_artifact(
        "human_review_design", {"test_point_set": {"test_points": [{"id": "TP-001"}]}}
    )
    assert reviewed_points["artifact_type"] == "test_point_set"

    cases = _event_artifact(
        "test_case_generation_agent", {"test_cases": [{"id": "TC-A-001"}]}
    )
    assert cases["artifact_type"] == "test_case_set"

    report = _event_artifact("coverage_check", {"coverage_report": {"total_cases": 1}})
    assert report["artifact_type"] == "coverage_report"


@pytest.mark.asyncio
async def test_retrieve_knowledge_saves_context_artifact(
    test_env, db_session, make_user, make_workflow, make_knowledge_base, monkeypatch
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
    # 真实的知识库记录：workflows.knowledge_base_id 有外键约束（SQLite 已开启 FK 校验）
    kb = make_knowledge_base(user.id, name="登录需求库")
    workflow = make_workflow(user.id, knowledge_base_id=kb.id)
    state = {
        "workflow_id": workflow.id,
        "user_id": workflow.user_id,
        "knowledge_base_id": kb.id,
        "requirement_text": "手机号验证码登录",
    }
    result = await workflow_nodes.retrieve_knowledge(state)

    assert len(result["retrieved_documents"]) == 2
    artifact = workflow_service.get_latest_artifact(workflow.id, "retrieved_context")
    assert artifact is not None
    content = json.loads(artifact.content)
    assert content["knowledge_base_id"] == kb.id
    assert content["documents"][0]["source"] == "《登录需求.pdf》 第3页"


def test_truncated_case_output_is_flagged_in_artifact(logged_in_client, stub_workflow_llm):
    """输出被截断并抢救出部分用例时，test_case_set 产物必须带 truncated 标记"""
    stub_workflow_llm.truncated = True
    workflow = _create_workflow(logged_in_client)
    _run_to_completion(logged_in_client, workflow["id"])

    detail = logged_in_client.get(f"/api/workflows/{workflow['id']}").json()
    cases_artifact = next(a for a in detail["artifacts"] if a["artifact_type"] == "test_case_set")
    assert cases_artifact["content"]["truncated"] is True
    # 2 个测试点 × 每批桩返回的 2 条用例
    assert len(cases_artifact["content"]["test_cases"]) == 4


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
    workflow_api._run_hub.finish("wf-dedupe", run1)


@pytest.mark.asyncio
async def test_background_run_publishes_and_subscriber_replays(monkeypatch):
    """后台运行发布事件；订阅者（含晚接入的）能重放并收到结束标记"""
    import asyncio

    from api.endpoints import workflow_api

    async def fake_runner(workflow_id, run_input, config_extra=None):
        run = workflow_api._run_hub.get(workflow_id)
        workflow_api._run_hub.publish(run, {"event": "node_done", "node": "load_requirement"})
        workflow_api._run_hub.publish(run, {"event": "completed"})
        # 模拟真实 runner 的 finally：发结束哨兵并清理注册表
        workflow_api._run_hub.finish(workflow_id, run)

    monkeypatch.setattr(workflow_api, "_run_workflow_graph", fake_runner)

    run = workflow_api._start_run("wf-sub", {"workflow_id": "wf-sub"})
    queue = workflow_api._run_hub.subscribe(run)
    events = []
    async for chunk in workflow_api._run_hub.forward_events(run, queue):
        for line in chunk.split("\n\n"):
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                events.append(line)

    assert any("node_done" in e for e in events)
    assert any("completed" in e for e in events)
    # 运行结束后清理注册表
    await run.task
    assert workflow_api._run_hub.get("wf-sub") is None


def test_create_workflow_rejects_overlong_requirement(logged_in_client):
    """需求正文上限 5 万字符：超长直接 422，避免撑爆分析/生成提示词预算"""
    response = logged_in_client.post(
        "/api/workflows",
        json={"name": "超长需求", "requirement_text": "需求" * 30_000},
    )
    assert response.status_code == 422


def test_start_rejected_when_waiting_review(logged_in_client, stub_workflow_llm):
    """waiting_review 的图停在 interrupt：/start 以空 resume 续跑属 langgraph 边界行为，须明确 409"""
    workflow = _create_workflow(logged_in_client)
    events = _start(logged_in_client, workflow["id"])
    assert any(e.get("event") == "waiting_review" for e in events)

    again = logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
    assert again.status_code == 409
    assert "人工确认" in again.json()["error"]

    # 确认流程不受影响：两个确认点依次确认后完成
    assert any(e.get("event") == "waiting_review" for e in _approve(logged_in_client, workflow["id"]))
    assert any(e.get("event") == "completed" for e in _approve(logged_in_client, workflow["id"]))


# ---------- 运行实例互斥：状态与进程内运行不一致的收尾窗口 ----------


def test_approve_rejected_while_run_active(logged_in_client, stub_workflow_llm):
    """已有运行实例时再次确认必须 409：此前返回 200 但 resume 载荷被静默丢弃"""
    from api.endpoints import workflow_api

    workflow = _create_workflow(logged_in_client)
    start_response = logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
    assert start_response.status_code == 200
    assert any(e.get("event") == "waiting_review" for e in parse_sse_events(start_response.text))

    # 模拟"运行实例仍存活"（真实场景：resume 正在重放 human_review，状态已是 waiting_review）
    handle = workflow_api._run_hub.register(workflow["id"], lambda: None)
    try:
        response = logged_in_client.post(
            f"/api/workflows/{workflow['id']}/approve", json={"analysis": None}
        )
        assert response.status_code == 409
        assert "运行中" in response.json()["error"]
    finally:
        workflow_api._run_hub.finish(workflow["id"], handle)


def test_start_rejected_when_run_active_but_status_failed(logged_in_client):
    """收尾窗口（DB 状态 failed、进程内仍有运行）不得再次抢占：否则状态被改却没有新任务"""
    from api.endpoints import workflow_api
    from services import workflow_service

    workflow = _create_workflow(logged_in_client)
    workflow_service.update_workflow_status(workflow["id"], status="failed", error="模拟收尾窗口")

    handle = workflow_api._run_hub.register(workflow["id"], lambda: None)
    try:
        response = logged_in_client.post(f"/api/workflows/{workflow['id']}/start")
        assert response.status_code == 409
        assert "运行中" in response.json()["error"]
    finally:
        workflow_api._run_hub.finish(workflow["id"], handle)
