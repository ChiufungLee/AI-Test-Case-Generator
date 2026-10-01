"""测试用例集资产服务：发布、查看、编辑、版本管理（测试工作台）"""

import json
import logging
from collections import Counter

from pydantic import ValidationError
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from models.database import create_session
from models.test_asset_models import TestCaseSet, TestCaseSetVersion
from models.user import User
from models.workflow_models import Artifact, Workflow
from schemas.workflow_schemas import TestCaseSet as TestCaseSetSchema

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """资源不存在或不可见（端点转 404）"""


class ConflictError(Exception):
    """状态或版本冲突（端点转 409）"""


# 不可变校验（D-015）适用的来源类型；publish/rollback 内容来自产物/历史版本，天然豁免
_IMMUTABLE_SOURCE_TYPES = ("manual_edit", "ai_edit")

# diff 参与逐字段比较的用例字段（除 id 外全部）
_DIFF_FIELDS = (
    "title",
    "preconditions",
    "steps",
    "expected_results",
    "priority",
    "automation",
    "requirement_refs",
    "rationale",
)


# ---------- 校验辅助 ----------


def ensure_unique_case_ids(cases: list[dict]) -> None:
    """用例编号在集合内必须唯一（D-010），重复时抛 ValueError"""
    counts = Counter(case.get("id") for case in cases)
    duplicates = sorted(case_id for case_id, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError("用例编号重复：" + "、".join(duplicates))


def _parse_json(raw) -> dict:
    try:
        content = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("用例集内容不是合法 JSON") from e
    if not isinstance(content, dict):
        raise ValueError("用例集内容必须是对象")
    return content


def _validated_content(content) -> dict:
    """校验并规范化用例集内容，接受 JSON 文本或 dict，返回 model_dump 后的 dict"""
    if isinstance(content, (str, bytes)):
        content = _parse_json(content)
    try:
        validated = TestCaseSetSchema.model_validate(content)
    except ValidationError as e:
        raise ValueError(f"测试用例集内容未通过校验：{e}") from e
    dumped = validated.model_dump()
    ensure_unique_case_ids(dumped["test_cases"])
    return dumped


def _assert_case_ids_immutable(base_content: dict, new_content: dict, deleted_case_ids: list[str]) -> None:
    """case_id 不可变校验（D-015）：未显式声明的用例移除一律拒绝，防意外重编号"""
    base_ids = {case.get("id") for case in base_content["test_cases"]}
    new_ids = {case.get("id") for case in new_content["test_cases"]}
    declared = set(deleted_case_ids)

    unknown = declared - base_ids
    if unknown:
        raise ValueError("声明的删除用例不存在于当前版本：" + "、".join(sorted(unknown)))

    contradictory = declared & new_ids
    if contradictory:
        raise ValueError("声明的删除用例仍存在于新内容中：" + "、".join(sorted(contradictory)))

    undeclared = base_ids - new_ids - declared
    if undeclared:
        raise ValueError(
            "已有用例编号不可修改，以下用例被移除但未显式声明删除：" + "、".join(sorted(undeclared))
        )


# ---------- 内部辅助 ----------


def _get_writable_asset(db, set_id: str, user_id: int) -> TestCaseSet:
    """取可写资产：不存在或他人 private 视为不存在（404 语义），shared 非 owner 抛 PermissionError"""
    asset = db.query(TestCaseSet).filter(TestCaseSet.id == set_id).first()
    if not asset or (asset.owner_user_id != user_id and asset.visibility != "shared"):
        raise NotFoundError("测试用例集不存在")
    if asset.owner_user_id != user_id:
        raise PermissionError("只有创建者可以修改该测试用例集")
    return asset


def _append_version(
    db,
    asset: TestCaseSet,
    content: dict,
    base_version: int,
    source_type: str,
    created_by: int,
    note: str | None = None,
    source_artifact_id: str | None = None,
    source_version_id: str | None = None,
    deleted_case_ids: list[str] | None = None,
) -> TestCaseSetVersion:
    """在乐观锁保护下为资产追加新版本。

    任何新版本都从 base_version 对应的当前版本产生（D-014）：parent_version_id
    由服务层内部推导，调用方不可指定；条件更新失败即并发冲突（D-008）。
    """
    base_row = (
        db.query(TestCaseSetVersion)
        .filter(
            TestCaseSetVersion.test_case_set_id == asset.id,
            TestCaseSetVersion.version == base_version,
        )
        .first()
    )
    if base_row is None:
        raise ConflictError(f"基线版本 v{base_version} 不存在")

    if source_type in _IMMUTABLE_SOURCE_TYPES:
        base_content = _validated_content(base_row.content)
        _assert_case_ids_immutable(base_content, content, deleted_case_ids or [])

    updated = (
        db.query(TestCaseSet)
        .filter(TestCaseSet.id == asset.id, TestCaseSet.current_version == base_version)
        .update(
            {"current_version": base_version + 1, "case_count": len(content["test_cases"])},
            synchronize_session=False,
        )
    )
    if updated == 0:
        raise ConflictError("用例集已被其他人修改，请刷新后重试")

    version = TestCaseSetVersion(
        test_case_set_id=asset.id,
        version=base_version + 1,
        parent_version_id=base_row.id,
        content=json.dumps(content, ensure_ascii=False),
        source_type=source_type,
        source_artifact_id=source_artifact_id,
        source_version_id=source_version_id,
        note=note,
        created_by=created_by,
    )
    db.add(version)
    return version


# ---------- 发布 ----------


def publish_from_workflow(user_id: int, workflow_id: str) -> tuple[TestCaseSet, TestCaseSetVersion]:
    """把工作流最新的 test_case_set 产物发布为测试用例集资产。

    首次发布创建资产 v1；同一工作流已有关联资产时追加新版本（D-007），
    唯一约束兜底并发发布（D-013）。
    """
    db = create_session()
    try:
        workflow = (
            db.query(Workflow)
            .filter(Workflow.id == workflow_id, Workflow.user_id == user_id)
            .first()
        )
        if not workflow:
            raise NotFoundError("任务不存在")
        if workflow.status != "completed":
            raise ConflictError("任务尚未完成，无法发布测试用例")

        artifact = (
            db.query(Artifact)
            .filter(
                Artifact.workflow_id == workflow_id,
                Artifact.artifact_type == "test_case_set",
            )
            .order_by(Artifact.version.desc())
            .first()
        )
        if not artifact:
            raise NotFoundError("未找到测试用例产物")

        content = _validated_content(artifact.content)

        asset = (
            db.query(TestCaseSet)
            .filter(TestCaseSet.source_workflow_id == workflow_id)
            .first()
        )
        if asset is None:
            asset = TestCaseSet(
                owner_user_id=user_id,
                name=workflow.name,
                description="",
                source_workflow_id=workflow_id,
                visibility="private",
                current_version=1,
                case_count=len(content["test_cases"]),
            )
            db.add(asset)
            try:
                db.flush()  # 先 flush 取得 asset.id，供版本行建立外键引用
                version = TestCaseSetVersion(
                    test_case_set_id=asset.id,
                    version=1,
                    parent_version_id=None,
                    content=json.dumps(content, ensure_ascii=False),
                    source_type="publish",
                    source_artifact_id=artifact.id,
                    created_by=user_id,
                )
                db.add(version)
                db.commit()
            except IntegrityError:
                # 并发发布同一工作流：唯一约束兜底（D-013）
                db.rollback()
                raise ConflictError("并发发布冲突，请刷新后重试")
            db.refresh(asset)
            db.refresh(version)
            logger.info("工作流 %s 发布测试用例集 %s v1", workflow_id, asset.id)
            return asset, version

        version = _append_version(
            db,
            asset,
            content,
            base_version=asset.current_version,
            source_type="publish",
            created_by=user_id,
            source_artifact_id=artifact.id,
        )
        db.commit()
        db.refresh(asset)
        db.refresh(version)
        logger.info("工作流 %s 重发布测试用例集 %s v%s", workflow_id, asset.id, version.version)
        return asset, version
    except (NotFoundError, ConflictError, PermissionError, ValueError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("发布测试用例集失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


# ---------- 查询 ----------


def list_test_sets(user_id: int) -> list[dict]:
    """当前用户可见的用例集（自己的 + 共享的），附创建者用户名"""
    db = create_session()
    try:
        rows = (
            db.query(TestCaseSet, User.username)
            .join(User, TestCaseSet.owner_user_id == User.id)
            .filter(or_(TestCaseSet.owner_user_id == user_id, TestCaseSet.visibility == "shared"))
            .order_by(TestCaseSet.updated_at.desc())
            .all()
        )
        return [
            {
                "id": asset.id,
                "name": asset.name,
                "description": asset.description,
                "visibility": asset.visibility,
                "owner_user_id": asset.owner_user_id,
                "owner_username": username,
                "is_mine": asset.owner_user_id == user_id,
                "source_workflow_id": asset.source_workflow_id,
                "current_version": asset.current_version,
                "case_count": asset.case_count,
                "created_at": asset.created_at,
                "updated_at": asset.updated_at,
            }
            for asset, username in rows
        ]
    finally:
        db.close()


def get_test_set(set_id: str, user_id: int, allow_shared_read: bool = True) -> TestCaseSet | None:
    """获取可见的用例集：自己的或共享的；不可见返回 None（404 语义）"""
    db = create_session()
    try:
        query = db.query(TestCaseSet).filter(TestCaseSet.id == set_id)
        if allow_shared_read:
            query = query.filter(
                or_(TestCaseSet.owner_user_id == user_id, TestCaseSet.visibility == "shared")
            )
        else:
            query = query.filter(TestCaseSet.owner_user_id == user_id)
        return query.first()
    finally:
        db.close()


def list_versions(set_id: str) -> list[TestCaseSetVersion]:
    db = create_session()
    try:
        return (
            db.query(TestCaseSetVersion)
            .filter(TestCaseSetVersion.test_case_set_id == set_id)
            .order_by(TestCaseSetVersion.version.desc())
            .all()
        )
    finally:
        db.close()


def get_version(set_id: str, version: int) -> TestCaseSetVersion | None:
    db = create_session()
    try:
        return (
            db.query(TestCaseSetVersion)
            .filter(
                TestCaseSetVersion.test_case_set_id == set_id,
                TestCaseSetVersion.version == version,
            )
            .first()
        )
    finally:
        db.close()


# ---------- 视图（供端点直接返回的 dict 载荷） ----------


def get_username(user_id: int) -> str | None:
    db = create_session()
    try:
        return db.query(User.username).filter(User.id == user_id).scalar()
    finally:
        db.close()


def asset_payload(asset: TestCaseSet, owner_username: str | None = None, is_mine: bool = False) -> dict:
    return {
        "id": asset.id,
        "name": asset.name,
        "description": asset.description,
        "visibility": asset.visibility,
        "owner_user_id": asset.owner_user_id,
        "owner_username": owner_username,
        "is_mine": is_mine,
        "source_workflow_id": asset.source_workflow_id,
        "current_version": asset.current_version,
        "case_count": asset.case_count,
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
    }


def version_payload(version: TestCaseSetVersion, include_content: bool = True) -> dict:
    data = {
        "id": version.id,
        "version": version.version,
        "parent_version_id": version.parent_version_id,
        "source_version_id": version.source_version_id,
        "source_type": version.source_type,
        "source_artifact_id": version.source_artifact_id,
        "note": version.note,
        "created_by": version.created_by,
        "created_at": version.created_at,
    }
    if include_content:
        try:
            data["content"] = json.loads(version.content)
        except (TypeError, ValueError):
            data["content"] = {"raw": version.content}
    return data


def get_test_set_view(set_id: str, user_id: int) -> dict | None:
    """详情视图：资产 + 创建者用户名 + 当前版本内容；不可见返回 None"""
    db = create_session()
    try:
        row = (
            db.query(TestCaseSet, User.username, TestCaseSetVersion)
            .join(User, TestCaseSet.owner_user_id == User.id)
            .outerjoin(
                TestCaseSetVersion,
                and_(
                    TestCaseSetVersion.test_case_set_id == TestCaseSet.id,
                    TestCaseSetVersion.version == TestCaseSet.current_version,
                ),
            )
            .filter(TestCaseSet.id == set_id)
            .filter(or_(TestCaseSet.owner_user_id == user_id, TestCaseSet.visibility == "shared"))
            .first()
        )
        if not row:
            return None
        asset, username, latest = row
        payload = asset_payload(asset, owner_username=username, is_mine=asset.owner_user_id == user_id)
        payload["current_content"] = version_payload(latest)["content"] if latest is not None else None
        return payload
    finally:
        db.close()


def list_version_views(set_id: str) -> list[dict]:
    """版本列表视图（不含 content）"""
    db = create_session()
    try:
        rows = (
            db.query(TestCaseSetVersion)
            .filter(TestCaseSetVersion.test_case_set_id == set_id)
            .order_by(TestCaseSetVersion.version.desc())
            .all()
        )
        return [version_payload(row, include_content=False) for row in rows]
    finally:
        db.close()


def get_version_view(set_id: str, version: int) -> dict | None:
    db = create_session()
    try:
        row = (
            db.query(TestCaseSetVersion)
            .filter(
                TestCaseSetVersion.test_case_set_id == set_id,
                TestCaseSetVersion.version == version,
            )
            .first()
        )
        return version_payload(row) if row else None
    finally:
        db.close()


# ---------- 编辑与版本 ----------


def save_new_version(
    set_id: str,
    user_id: int,
    content: dict,
    base_version: int,
    source_type: str,
    note: str | None = None,
    source_artifact_id: str | None = None,
    source_version_id: str | None = None,
    deleted_case_ids: list[str] | None = None,
) -> TestCaseSetVersion:
    """为用例集保存新版本（全量替换 + base_version 乐观锁，D-008/D-014/D-015）。

    manual_edit/ai_edit 强制 case_id 不可变校验；publish/rollback 豁免。
    """
    validated = _validated_content(content)
    db = create_session()
    try:
        asset = _get_writable_asset(db, set_id, user_id)
        version = _append_version(
            db,
            asset,
            validated,
            base_version=base_version,
            source_type=source_type,
            created_by=user_id,
            note=note,
            source_artifact_id=source_artifact_id,
            source_version_id=source_version_id,
            deleted_case_ids=deleted_case_ids,
        )
        db.commit()
        db.refresh(version)
        logger.info("测试用例集 %s 保存新版本 v%s (%s)", set_id, version.version, source_type)
        return version
    except (NotFoundError, ConflictError, PermissionError, ValueError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("保存测试用例集版本失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def rollback_version(set_id: str, user_id: int, source_version: int, note: str | None = None) -> TestCaseSetVersion:
    """把历史版本的内容另存为新版本（source_type=rollback），历史版本不改写。

    版本链保持线性：新版本 parent 为当前最新版本，source_version_id 记录内容来源（D-014）。
    """
    db = create_session()
    try:
        asset = _get_writable_asset(db, set_id, user_id)
        source_row = (
            db.query(TestCaseSetVersion)
            .filter(
                TestCaseSetVersion.test_case_set_id == set_id,
                TestCaseSetVersion.version == source_version,
            )
            .first()
        )
        if source_row is None:
            raise NotFoundError("要回滚的版本不存在")
        if source_row.version == asset.current_version:
            raise ConflictError("该版本已是当前版本，无需回滚")

        content = _validated_content(source_row.content)
        version = _append_version(
            db,
            asset,
            content,
            base_version=asset.current_version,
            source_type="rollback",
            created_by=user_id,
            note=note if note is not None else f"回滚自 v{source_version}",
            source_version_id=source_row.id,
        )
        db.commit()
        db.refresh(version)
        logger.info("测试用例集 %s 从 v%s 回滚为新版本 v%s", set_id, source_version, version.version)
        return version
    except (NotFoundError, ConflictError, PermissionError, ValueError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("回滚测试用例集版本失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def diff_versions(set_id: str, from_version: int, to_version: int) -> dict:
    """按 case_id 对比两个版本，返回 added/removed/changed（确定性计算）"""
    db = create_session()
    try:
        rows = (
            db.query(TestCaseSetVersion)
            .filter(
                TestCaseSetVersion.test_case_set_id == set_id,
                TestCaseSetVersion.version.in_([from_version, to_version]),
            )
            .all()
        )
        by_version = {row.version: row for row in rows}
        from_row = by_version.get(from_version)
        to_row = by_version.get(to_version)
        if from_row is None or to_row is None:
            raise NotFoundError("要对比的版本不存在")

        from_cases = {case["id"]: case for case in _validated_content(from_row.content)["test_cases"]}
        to_cases = {case["id"]: case for case in _validated_content(to_row.content)["test_cases"]}

        added = [to_cases[case_id] for case_id in sorted(to_cases.keys() - from_cases.keys())]
        removed = [from_cases[case_id] for case_id in sorted(from_cases.keys() - to_cases.keys())]
        changed = []
        for case_id in sorted(from_cases.keys() & to_cases.keys()):
            fields = {}
            for field in _DIFF_FIELDS:
                before, after = from_cases[case_id].get(field), to_cases[case_id].get(field)
                if before != after:
                    fields[field] = {"before": before, "after": after}
            if fields:
                changed.append(
                    {"case_id": case_id, "title": to_cases[case_id].get("title", ""), "fields": fields}
                )
        return {
            "from_version": from_version,
            "to_version": to_version,
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    finally:
        db.close()


# ---------- 元信息与删除 ----------


def update_meta(
    set_id: str,
    user_id: int,
    name: str | None = None,
    description: str | None = None,
    visibility: str | None = None,
) -> TestCaseSet:
    """更新元信息（不产生新版本），owner-only"""
    db = create_session()
    try:
        asset = _get_writable_asset(db, set_id, user_id)
        if name is not None:
            asset.name = name
        if description is not None:
            asset.description = description
        if visibility is not None:
            asset.visibility = visibility
        db.commit()
        db.refresh(asset)
        return asset
    except (NotFoundError, PermissionError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("更新测试用例集元信息失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def delete_test_set(set_id: str, user_id: int) -> None:
    """删除用例集（版本级联删除），owner-only"""
    db = create_session()
    try:
        asset = _get_writable_asset(db, set_id, user_id)
        db.delete(asset)
        db.commit()
        logger.info("测试用例集 %s 已删除", set_id)
    except (NotFoundError, PermissionError):
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("删除测试用例集失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()
