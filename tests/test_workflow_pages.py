"""测试任务页面拆分（列表页 / 新建页 / 详情页）与列表搜索、分页的回归测试。

D-037：菜单进入列表页，任务详情独立成页，新建表单独立成页。
"""

from datetime import datetime

from api.endpoints import workflow_api
from models.test_asset_models import TestCaseSet as CaseSetModel
from models.user import User
from models.workflow_models import Artifact, Workflow
from services import workflow_service


def _alice_id(db_session) -> int:
    return db_session.query(User).filter(User.username == "alice").first().id


def _make_workflow_at(make_workflow, db_session, user_id, name, requirement_text, created_at):
    """按指定创建时间落库：分页顺序断言需要确定性的 created_at（同秒创建会退化为 id 序）"""
    workflow = make_workflow(user_id, name=name, requirement_text=requirement_text)
    workflow.created_at = created_at
    db_session.commit()
    return workflow


# ---------- 页面路由 ----------


def test_pages_redirect_to_login_when_unauthenticated(client):
    for path in ("/workflows", "/workflow-new", "/workflow-detail?workflow_id=whatever"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"


def test_workflow_list_page_renders_list_layout(logged_in_client):
    """列表页只做列表：搜索框 + 新建入口，不再内嵌新建表单与详情面板"""
    response = logged_in_client.get("/workflows")

    assert response.status_code == 200
    assert 'id="searchInput"' in response.text
    assert 'href="/workflow-new"' in response.text
    assert "/static/js/workflow_list.js" in response.text
    assert 'id="workflowRequirement"' not in response.text
    assert 'id="stepBar"' not in response.text


def _between(html: str, start_marker: str, end_marker: str) -> str:
    """截取 start_marker 到其后第一个 end_marker 之间的片段（断言按钮落在哪个容器里）"""
    start = html.index(start_marker)
    return html[start : html.index(end_marker, start)]


def test_workflow_new_page_renders_create_form(logged_in_client):
    response = logged_in_client.get("/workflow-new")

    assert response.status_code == 200
    assert "/static/js/workflow_new.js" in response.text
    assert 'id="workflowRequirement"' in response.text
    assert 'id="createWorkflowBtn"' in response.text
    assert 'id="stepBar"' not in response.text
    # form-page 是超宽屏封顶居中的挂钩（workflow.css 的 .form-page 规则）
    assert '<main class="main-content form-page">' in response.text
    # 返回列表按钮与用例集详情页同款式（.back-btn），且与标题同一行（标题行右侧）
    header = _between(response.text, '<header class="page-header">', "</header>")
    assert "page-title" in header
    assert '<a class="back-btn" href="/workflows"' in header


def test_workflow_detail_page_injects_workflow_id(logged_in_client, make_workflow, db_session):
    workflow = make_workflow(_alice_id(db_session), name="登录任务")

    response = logged_in_client.get(f"/workflow-detail?workflow_id={workflow.id}")

    assert response.status_code == 200
    assert f'window.workflowId = "{workflow.id}"' in response.text
    assert "/static/js/workflow_detail.js" in response.text
    assert 'id="stepBar"' in response.text
    assert 'id="requirementSource"' in response.text
    # 返回列表按钮落在任务标题行内（与标题、状态徽章同一行，靠右）
    header = _between(response.text, '<div class="detail-header">', "</div>")
    assert 'id="wfTitle"' in header
    assert '<a class="back-btn" href="/workflows"' in header
    # 详情页不承载新建表单
    assert 'id="workflowRequirement"' not in response.text


def test_workflow_detail_page_redirects_for_other_users_task(
    logged_in_client, make_user, make_workflow
):
    bob = make_user("bob", "secret123")
    workflow = make_workflow(bob.id, name="bob 的任务")

    response = logged_in_client.get(
        f"/workflow-detail?workflow_id={workflow.id}", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/workflows"


def test_workflow_detail_page_redirects_for_unknown_task(logged_in_client):
    response = logged_in_client.get(
        "/workflow-detail?workflow_id=not-exist", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/workflows"


# ---------- 列表搜索与分页 ----------


def test_list_workflows_searches_name_and_requirement(logged_in_client, make_workflow, db_session):
    alice = _alice_id(db_session)
    make_workflow(alice, name="登录功能测试", requirement_text="需求：新增手机号验证码登录")
    make_workflow(alice, name="支付流程测试", requirement_text="需求：支持余额支付与退款")

    by_name = logged_in_client.get("/api/workflows", params={"keyword": "支付"}).json()
    assert by_name["total"] == 1
    assert [w["name"] for w in by_name["workflows"]] == ["支付流程测试"]

    # 需求原文同样参与匹配
    by_requirement = logged_in_client.get("/api/workflows", params={"keyword": "验证码"}).json()
    assert [w["name"] for w in by_requirement["workflows"]] == ["登录功能测试"]

    assert logged_in_client.get("/api/workflows", params={"keyword": "不存在的词"}).json() == {
        "workflows": [],
        "total": 0,
        "limit": None,
        "offset": 0,
    }


def test_list_workflows_keyword_matches_ascii_case_insensitively(
    logged_in_client, make_workflow, db_session
):
    alice = _alice_id(db_session)
    make_workflow(alice, name="Login Flow", requirement_text="requirement: add SMS login")

    result = logged_in_client.get("/api/workflows", params={"keyword": "login flow"}).json()

    assert [w["name"] for w in result["workflows"]] == ["Login Flow"]


def test_list_workflows_ignores_blank_keyword(logged_in_client, make_workflow, db_session):
    alice = _alice_id(db_session)
    make_workflow(alice, name="任务一")

    result = logged_in_client.get("/api/workflows", params={"keyword": "   "}).json()

    assert result["total"] == 1


def test_list_workflows_treats_wildcards_as_literal(logged_in_client, make_workflow, db_session):
    """用户输入 % / _ 应按字面量匹配，不能变成"匹配任意内容"的 LIKE 通配符"""
    alice = _alice_id(db_session)
    make_workflow(alice, name="覆盖率 100% 校验")
    make_workflow(alice, name="普通任务")

    result = logged_in_client.get("/api/workflows", params={"keyword": "%"}).json()

    assert [w["name"] for w in result["workflows"]] == ["覆盖率 100% 校验"]


def test_list_workflows_paginates_newest_first(logged_in_client, make_workflow, db_session):
    alice = _alice_id(db_session)
    base = datetime(2026, 1, 1, 10, 0, 0)
    for index in range(5):
        _make_workflow_at(
            make_workflow,
            db_session,
            alice,
            name=f"任务 {index}",
            requirement_text="需求：分页测试",
            created_at=base.replace(minute=index),
        )

    first = logged_in_client.get("/api/workflows", params={"limit": 2, "offset": 0}).json()
    assert first["total"] == 5
    assert first["limit"] == 2
    assert first["offset"] == 0
    assert [w["name"] for w in first["workflows"]] == ["任务 4", "任务 3"]

    second = logged_in_client.get("/api/workflows", params={"limit": 2, "offset": 2}).json()
    assert [w["name"] for w in second["workflows"]] == ["任务 2", "任务 1"]

    last = logged_in_client.get("/api/workflows", params={"limit": 2, "offset": 4}).json()
    assert [w["name"] for w in last["workflows"]] == ["任务 0"]
    # total 始终是命中总数，不随分页变化
    assert last["total"] == 5


def test_list_workflows_pagination_search_compose(logged_in_client, make_workflow, db_session):
    alice = _alice_id(db_session)
    base = datetime(2026, 2, 1, 9, 0, 0)
    for index in range(3):
        _make_workflow_at(
            make_workflow,
            db_session,
            alice,
            name=f"登录任务 {index}",
            requirement_text="需求：登录",
            created_at=base.replace(minute=index),
        )
    _make_workflow_at(
        make_workflow,
        db_session,
        alice,
        name="支付任务",
        requirement_text="需求：支付",
        created_at=base.replace(minute=30),
    )

    result = logged_in_client.get(
        "/api/workflows", params={"keyword": "登录", "limit": 2, "offset": 1}
    ).json()

    assert result["total"] == 3
    assert [w["name"] for w in result["workflows"]] == ["登录任务 1", "登录任务 0"]


def test_list_workflows_rejects_invalid_pagination(logged_in_client):
    assert logged_in_client.get("/api/workflows", params={"limit": 0}).status_code == 422
    assert logged_in_client.get("/api/workflows", params={"limit": 101}).status_code == 422
    assert logged_in_client.get("/api/workflows", params={"offset": -1}).status_code == 422


def test_list_workflows_only_returns_own_tasks(logged_in_client, make_user, make_workflow):
    bob = make_user("bob", "secret123")
    make_workflow(bob.id, name="bob 的登录任务", requirement_text="需求：登录")

    result = logged_in_client.get("/api/workflows", params={"keyword": "登录"}).json()

    assert result["total"] == 0
    assert result["workflows"] == []


def test_list_workflows_excerpt_is_trimmed(logged_in_client, make_workflow, db_session):
    """列表卡片只需要摘要：换行压平并截断，避免列表接口携带整篇需求"""
    alice = _alice_id(db_session)
    make_workflow(alice, name="长需求", requirement_text="第一行\n" * 100)

    workflow = logged_in_client.get("/api/workflows").json()["workflows"][0]
    excerpt = workflow["requirement_excerpt"]

    assert len(excerpt) <= 120
    assert "\n" not in excerpt
    assert excerpt == ("第一行 " * 100).strip()[:120]


def test_search_workflows_service_is_reusable(db_session, make_user):
    """服务层入口同时供端点与旧调用方使用：list_workflows 仍返回全量"""
    from services import workflow_service

    alice = make_user("search_owner", "secret123")
    workflow = Workflow(user_id=alice.id, name="服务层任务", requirement_text="需求：服务层")
    db_session.add(workflow)
    db_session.commit()

    assert [w.name for w in workflow_service.list_workflows(alice.id)] == ["服务层任务"]
    items, total = workflow_service.search_workflows(alice.id, keyword="服务层", limit=10, offset=0)
    assert total == 1
    assert [w.name for w in items] == ["服务层任务"]
    assert workflow_service.search_workflows(alice.id, keyword="没有")[1] == 0


# ---------- 列表页的重命名与删除 ----------


def test_rename_and_delete_require_login(client):
    assert client.patch("/api/workflows/x", json={"name": "新名称"}).status_code == 401
    assert client.delete("/api/workflows/x").status_code == 401


def test_rename_workflow_only_touches_the_name(logged_in_client, make_workflow, db_session):
    workflow = make_workflow(_alice_id(db_session), name="旧名称", requirement_text="需求：登录")

    response = logged_in_client.patch(
        f"/api/workflows/{workflow.id}", json={"name": "  新名称  "}
    )

    assert response.status_code == 200
    # 前后空白由服务端 strip 后再落库
    assert response.json()["name"] == "新名称"
    detail = logged_in_client.get(f"/api/workflows/{workflow.id}").json()
    assert detail["name"] == "新名称"
    assert detail["requirement_text"] == "需求：登录"


def test_rename_workflow_rejects_blank_and_overlong_name(logged_in_client, make_workflow, db_session):
    workflow = make_workflow(_alice_id(db_session), name="旧名称")

    blank = logged_in_client.patch(f"/api/workflows/{workflow.id}", json={"name": "   "})
    assert blank.status_code == 400
    assert blank.json()["error"] == "测试工作流名称不能为空"

    # 列宽 String(200)：超长名称由 pydantic 挡住，不落库
    assert (
        logged_in_client.patch(
            f"/api/workflows/{workflow.id}", json={"name": "长" * 201}
        ).status_code
        == 422
    )
    assert logged_in_client.get(f"/api/workflows/{workflow.id}").json()["name"] == "旧名称"


def test_rename_workflow_hides_other_users_workflow(logged_in_client, make_user, make_workflow):
    bob = make_user("bob", "secret123")
    others = make_workflow(bob.id, name="bob 的任务")

    assert (
        logged_in_client.patch(f"/api/workflows/{others.id}", json={"name": "改名"}).status_code
        == 404
    )
    assert (
        logged_in_client.patch("/api/workflows/not-exist", json={"name": "改名"}).status_code
        == 404
    )


def test_delete_workflow_removes_workflow_and_artifacts(
    logged_in_client, make_workflow, db_session
):
    workflow_id = make_workflow(_alice_id(db_session), name="待删除").id
    workflow_service.save_artifact(workflow_id, "requirement_analysis", {"summary": "分析"})
    workflow_service.save_artifact(workflow_id, "test_case_set", {"test_cases": []})
    assert db_session.query(Artifact).filter(Artifact.workflow_id == workflow_id).count() == 2

    response = logged_in_client.delete(f"/api/workflows/{workflow_id}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert logged_in_client.get(f"/api/workflows/{workflow_id}").status_code == 404
    # 用 count() 断言：db_session 的 identity map 里还挂着这个（已过期的）实例，
    # .first() 会因主键行消失而抛 ObjectDeletedError 而不是返回 None
    assert db_session.query(Workflow).filter(Workflow.id == workflow_id).count() == 0
    # 产物随任务级联删除（ORM delete-orphan + 外键 CASCADE 双保险）
    assert db_session.query(Artifact).filter(Artifact.workflow_id == workflow_id).count() == 0


def test_delete_workflow_hides_other_users_workflow(
    logged_in_client, make_user, make_workflow, db_session
):
    bob = make_user("bob", "secret123")
    others = make_workflow(bob.id, name="bob 的任务")

    assert logged_in_client.delete(f"/api/workflows/{others.id}").status_code == 404
    assert logged_in_client.delete("/api/workflows/not-exist").status_code == 404
    db_session.expire_all()
    assert db_session.query(Workflow).filter(Workflow.id == others.id).first() is not None


def test_delete_workflow_keeps_published_test_case_set(
    logged_in_client, make_workflow, make_test_case_set, db_session
):
    """用例集是长期资产：删工作流只把溯源字段置空，不跟着删资产"""
    alice = _alice_id(db_session)
    workflow = make_workflow(alice, name="已沉淀用例的任务")
    asset = make_test_case_set(alice, name="来自工作流的用例集", source_workflow_id=workflow.id)

    assert logged_in_client.delete(f"/api/workflows/{workflow.id}").status_code == 200

    db_session.expire_all()
    kept = db_session.query(CaseSetModel).filter(CaseSetModel.id == asset.id).first()
    assert kept is not None
    assert kept.source_workflow_id is None


def test_delete_workflow_rejected_while_running(logged_in_client, make_workflow, db_session):
    workflow = make_workflow(_alice_id(db_session), name="跑着的任务", status="generating")
    handle = workflow_api._run_hub.register(workflow.id, lambda: None)
    try:
        response = logged_in_client.delete(f"/api/workflows/{workflow.id}")

        assert response.status_code == 409
        assert "运行中" in response.json()["error"]
        assert logged_in_client.get(f"/api/workflows/{workflow.id}").status_code == 200

        # 改名只是元数据，运行中照样能改（不碰产物与 checkpoint）
        renamed = logged_in_client.patch(f"/api/workflows/{workflow.id}", json={"name": "改名"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "改名"
    finally:
        workflow_api._run_hub.finish(workflow.id, handle)

    # 运行收尾后即可删除
    assert logged_in_client.delete(f"/api/workflows/{workflow.id}").status_code == 200
