"""测试用例集资产：服务层测试（API 端点测试追加在同文件）"""

import json

import pytest
from sqlalchemy.exc import IntegrityError

from models.test_asset_models import TestCaseSet as CaseSetModel
from models.user import User
from services import test_asset_service
from services.test_asset_service import ConflictError, NotFoundError
from services import workflow_service


@pytest.fixture()
def alice(db_session, make_user):
    """服务层测试不经过 HTTP，需要显式创建 alice，返回其 user_id"""
    make_user("alice", "secret123")
    return db_session.query(User).filter(User.username == "alice").first().id


@pytest.fixture()
def bob(db_session, make_user):
    make_user("bob", "secret123")
    return db_session.query(User).filter(User.username == "bob").first().id


def _cases(*entries) -> dict:
    """构造用例集内容；entries 为 (case_id, title) 元组，case_id 与标题解耦"""
    return {
        "test_cases": [
            {
                "id": case_id,
                "title": title,
                "preconditions": ["用户已注册"],
                "steps": ["打开登录页", "输入凭据"],
                "expected_results": ["结果符合预期"],
                "priority": "P1",
                "automation": "Manual",
                "requirement_refs": ["REQ-001"],
                "rationale": "",
            }
            for case_id, title in entries
        ]
    }


V1_CASES = (("TC-AUTH-001", "登录成功"), ("TC-AUTH-002", "密码错误"))


def _case_ids(content: dict) -> list[str]:
    return [case["id"] for case in content["test_cases"]]


def _completed_workflow_with_artifact(db_session, make_workflow, user_id, cases=V1_CASES):
    workflow = make_workflow(user_id, status="completed")
    workflow_service.save_artifact(workflow.id, "test_case_set", _cases(*cases))
    return workflow


def _make_asset_with_v1(make_test_case_set, make_test_case_set_version, user_id):
    asset = make_test_case_set(user_id, case_count=2)
    v1 = make_test_case_set_version(asset.id, 1, _cases(*V1_CASES), source_type="publish")
    return asset, v1


# ---------- 发布 ----------


def test_publish_creates_asset_v1(db_session, make_workflow, alice):
    workflow = _completed_workflow_with_artifact(db_session, make_workflow, alice)

    asset, version = test_asset_service.publish_from_workflow(alice, workflow.id)

    assert asset.source_workflow_id == workflow.id
    assert asset.owner_user_id == alice
    assert asset.name == workflow.name
    assert asset.current_version == 1
    assert asset.case_count == 2
    assert version.version == 1
    assert version.source_type == "publish"
    assert version.source_artifact_id is not None
    assert version.parent_version_id is None
    assert _case_ids(json.loads(version.content)) == ["TC-AUTH-001", "TC-AUTH-002"]


def test_republish_appends_version(db_session, make_workflow, alice):
    workflow = _completed_workflow_with_artifact(db_session, make_workflow, alice)
    asset, v1 = test_asset_service.publish_from_workflow(alice, workflow.id)

    # 工作流重新生成用例（产物内容变化）后再次发布 → 追加 v2
    workflow_service.save_artifact(
        workflow.id, "test_case_set",
        _cases(("TC-AUTH-001", "登录成功"), ("TC-AUTH-002", "密码错误"), ("TC-AUTH-003", "验证码错误")),
    )
    asset, v2 = test_asset_service.publish_from_workflow(alice, workflow.id)

    assert asset.current_version == 2
    assert asset.case_count == 3
    assert v2.version == 2
    assert v2.parent_version_id == v1.id
    assert v2.source_type == "publish"
    assert v2.id != v1.id


def test_publish_requires_completed_workflow(db_session, make_workflow, alice):
    workflow = make_workflow(alice)  # status=created
    with pytest.raises(ConflictError):
        test_asset_service.publish_from_workflow(alice, workflow.id)


def test_publish_without_artifact_returns_not_found(db_session, make_workflow, alice):
    workflow = make_workflow(alice, status="completed")
    with pytest.raises(NotFoundError):
        test_asset_service.publish_from_workflow(alice, workflow.id)


def test_publish_other_users_workflow_returns_not_found(db_session, make_workflow, alice, bob):
    workflow = _completed_workflow_with_artifact(db_session, make_workflow, bob)
    with pytest.raises(NotFoundError):
        test_asset_service.publish_from_workflow(alice, workflow.id)


def test_publish_invalid_artifact_content_raises_value_error(db_session, make_workflow, alice):
    workflow = make_workflow(alice, status="completed")
    workflow_service.save_artifact(workflow.id, "test_case_set", {"test_cases": [{"id": "TC-001"}]})  # 缺 title

    with pytest.raises(ValueError):
        test_asset_service.publish_from_workflow(alice, workflow.id)


def test_publish_duplicate_case_ids_raises_value_error(db_session, make_workflow, alice):
    workflow = make_workflow(alice, status="completed")
    workflow_service.save_artifact(
        workflow.id,
        "test_case_set",
        {"test_cases": [
            {"id": "TC-001", "title": "用例一"},
            {"id": "TC-001", "title": "用例二"},
        ]},
    )

    with pytest.raises(ValueError):
        test_asset_service.publish_from_workflow(alice, workflow.id)


def test_unique_constraint_forbids_two_assets_per_workflow(db_session, make_workflow, alice):
    """D-013：同一 source_workflow_id 直接插入第二个资产必须被数据库拒绝"""
    workflow = _completed_workflow_with_artifact(db_session, make_workflow, alice)

    db_session.add(CaseSetModel(owner_user_id=alice, name="A", source_workflow_id=workflow.id))
    db_session.commit()
    db_session.add(CaseSetModel(owner_user_id=alice, name="B", source_workflow_id=workflow.id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------- 版本保存与乐观锁 ----------


def test_save_new_version_appends_and_bumps(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, v1 = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    version = test_asset_service.save_new_version(
        asset.id, alice,
        _cases(("TC-AUTH-001", "登录成功"), ("TC-AUTH-002", "密码错误"), ("TC-AUTH-003", "账号锁定")),
        base_version=1, source_type="manual_edit", note="补充锁定场景",
    )

    db_session.expire_all()
    assert version.version == 2
    assert version.parent_version_id == v1.id
    assert version.note == "补充锁定场景"
    assert db_session.query(CaseSetModel).filter(CaseSetModel.id == asset.id).first().current_version == 2


def test_save_new_version_stale_base_version_conflicts(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)
    same_cases = _cases(*V1_CASES)

    test_asset_service.save_new_version(asset.id, alice, same_cases, base_version=1, source_type="manual_edit")
    with pytest.raises(ConflictError):
        test_asset_service.save_new_version(asset.id, alice, same_cases, base_version=1, source_type="manual_edit")


def test_manual_edit_undeclared_deletion_rejected(db_session, make_test_case_set, make_test_case_set_version, alice):
    """D-015：未显式声明的用例移除必须拒绝"""
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    with pytest.raises(ValueError):
        test_asset_service.save_new_version(
            asset.id, alice, _cases(("TC-AUTH-001", "登录成功")),
            base_version=1, source_type="manual_edit",
        )


def test_manual_edit_declared_deletion_allowed(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    version = test_asset_service.save_new_version(
        asset.id, alice, _cases(("TC-AUTH-001", "登录成功")),
        base_version=1, source_type="manual_edit", deleted_case_ids=["TC-AUTH-002"],
    )

    assert version.version == 2
    assert _case_ids(json.loads(version.content)) == ["TC-AUTH-001"]


def test_manual_edit_unknown_deleted_id_rejected(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    with pytest.raises(ValueError):
        test_asset_service.save_new_version(
            asset.id, alice, _cases(("TC-AUTH-001", "登录成功")),
            base_version=1, source_type="manual_edit", deleted_case_ids=["TC-XXX-999"],
        )


def test_manual_edit_contradictory_deletion_rejected(db_session, make_test_case_set, make_test_case_set_version, alice):
    """声明删除但仍保留在新内容中，属于矛盾输入"""
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    with pytest.raises(ValueError):
        test_asset_service.save_new_version(
            asset.id, alice, _cases(*V1_CASES),
            base_version=1, source_type="manual_edit", deleted_case_ids=["TC-AUTH-002"],
        )


def test_publish_renumber_is_exempt_from_immutability(db_session, make_workflow, alice):
    """重新生成后编号全变是重发布的正常语义，不受不可变校验限制"""
    workflow = _completed_workflow_with_artifact(db_session, make_workflow, alice)
    asset, _ = test_asset_service.publish_from_workflow(alice, workflow.id)

    workflow_service.save_artifact(
        workflow.id, "test_case_set",
        _cases(("TC-NEW-001", "正常登录V2"), ("TC-NEW-002", "错误密码V2"), ("TC-NEW-003", "验证码错误V2")),
    )
    asset, v2 = test_asset_service.publish_from_workflow(alice, workflow.id)

    assert v2.version == 2
    assert all(case["id"].startswith("TC-NEW") for case in json.loads(v2.content)["test_cases"])


def test_rollback_keeps_linear_chain(db_session, make_test_case_set, make_test_case_set_version, alice):
    """D-014：回滚产生的版本 parent 为当前版本，内容来源记录在 source_version_id"""
    asset, v1 = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)
    test_asset_service.save_new_version(
        asset.id, alice, _cases(("TC-AUTH-001", "登录成功")),
        base_version=1, source_type="manual_edit", deleted_case_ids=["TC-AUTH-002"],
    )

    v3 = test_asset_service.rollback_version(asset.id, alice, 1)

    assert v3.version == 3
    assert v3.source_type == "rollback"
    assert v3.parent_version_id != v1.id  # parent 是 v2（当前版本），不是 v1
    assert _case_ids(json.loads(v3.content)) == ["TC-AUTH-001", "TC-AUTH-002"]

    versions = {row.version: row for row in test_asset_service.list_versions(asset.id)}
    assert versions[3].parent_version_id == versions[2].id
    assert versions[3].source_version_id == versions[1].id
    assert versions[2].parent_version_id == versions[1].id


def test_rollback_uses_default_note(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)
    test_asset_service.save_new_version(
        asset.id, alice, _cases(("TC-AUTH-001", "登录成功")),
        base_version=1, source_type="manual_edit", deleted_case_ids=["TC-AUTH-002"],
    )

    v3 = test_asset_service.rollback_version(asset.id, alice, 1)

    assert v3.note == "回滚自 v1"


def test_rollback_to_current_version_conflicts(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    with pytest.raises(ConflictError):
        test_asset_service.rollback_version(asset.id, alice, 1)


def test_rollback_to_missing_version_returns_not_found(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    with pytest.raises(NotFoundError):
        test_asset_service.rollback_version(asset.id, alice, 99)


# ---------- diff ----------


def test_diff_versions_reports_added_removed_changed(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)
    test_asset_service.save_new_version(
        asset.id, alice,
        _cases(("TC-AUTH-001", "登录成功V2"), ("TC-AUTH-003", "账号锁定")),
        base_version=1, source_type="manual_edit", deleted_case_ids=["TC-AUTH-002"],
    )

    diff = test_asset_service.diff_versions(asset.id, 1, 2)

    assert diff["added"] and diff["added"][0]["title"] == "账号锁定"
    assert diff["removed"] and diff["removed"][0]["title"] == "密码错误"
    assert len(diff["changed"]) == 1
    change = diff["changed"][0]
    assert change["case_id"] == "TC-AUTH-001"
    assert change["fields"]["title"] == {"before": "登录成功", "after": "登录成功V2"}


def test_diff_identical_versions_has_no_changes(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, v1 = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)
    test_asset_service.save_new_version(asset.id, alice, v1.content, base_version=1, source_type="manual_edit")

    diff = test_asset_service.diff_versions(asset.id, 1, 2)

    assert diff["added"] == [] and diff["removed"] == [] and diff["changed"] == []


def test_diff_missing_version_returns_not_found(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    with pytest.raises(NotFoundError):
        test_asset_service.diff_versions(asset.id, 1, 9)


# ---------- 可见性与元信息 ----------


def test_shared_asset_readable_by_others_but_not_writable(db_session, make_test_case_set, make_test_case_set_version, alice, bob):
    asset = make_test_case_set(alice, visibility="shared", case_count=2)
    make_test_case_set_version(asset.id, 1, _cases(*V1_CASES))

    readable = test_asset_service.get_test_set(asset.id, bob)
    assert readable is not None

    with pytest.raises(PermissionError):
        test_asset_service.save_new_version(asset.id, bob, _cases(("TC-AUTH-001", "改标题")), base_version=1, source_type="manual_edit")


def test_private_asset_invisible_to_others(db_session, make_test_case_set, make_test_case_set_version, alice, bob):
    asset = make_test_case_set(alice, case_count=2)
    make_test_case_set_version(asset.id, 1, _cases(*V1_CASES))

    assert test_asset_service.get_test_set(asset.id, bob) is None
    with pytest.raises(NotFoundError):
        test_asset_service.save_new_version(asset.id, bob, _cases(("TC-AUTH-001", "改标题")), base_version=1, source_type="manual_edit")


def test_list_test_sets_returns_own_and_shared(db_session, make_test_case_set, alice, bob):
    mine = make_test_case_set(alice, name="我的用例集")
    shared = make_test_case_set(bob, name="共享用例集", visibility="shared")
    make_test_case_set(bob, name="他人私有集")

    listed = {item["name"]: item for item in test_asset_service.list_test_sets(alice)}

    assert "我的用例集" in listed and listed["我的用例集"]["is_mine"] is True
    assert "共享用例集" in listed and listed["共享用例集"]["is_mine"] is False
    assert listed["共享用例集"]["owner_username"] == "bob"
    assert "他人私有集" not in listed
    assert mine.id and shared.id


def test_update_meta_changes_visibility(db_session, make_test_case_set, alice, bob):
    asset = make_test_case_set(alice)

    test_asset_service.update_meta(asset.id, alice, visibility="shared", description="团队共享")

    updated = test_asset_service.get_test_set(asset.id, bob)
    assert updated is not None and updated.visibility == "shared"


def test_update_meta_rejected_for_non_owner(db_session, make_test_case_set, alice, bob):
    asset = make_test_case_set(alice, visibility="shared")

    with pytest.raises(PermissionError):
        test_asset_service.update_meta(asset.id, bob, name="改名")


# ---------- 删除 ----------


def test_delete_removes_versions(db_session, make_test_case_set, make_test_case_set_version, alice):
    asset, _ = _make_asset_with_v1(make_test_case_set, make_test_case_set_version, alice)

    test_asset_service.delete_test_set(asset.id, alice)

    assert test_asset_service.get_test_set(asset.id, alice) is None
    assert test_asset_service.list_versions(asset.id) == []


def test_delete_rejected_for_non_owner(db_session, make_test_case_set, alice, bob):
    asset = make_test_case_set(alice, visibility="shared")

    with pytest.raises(PermissionError):
        test_asset_service.delete_test_set(asset.id, bob)
