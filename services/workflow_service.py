import json
import logging

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models.database import create_session
from models.workflow_models import Artifact, Workflow

logger = logging.getLogger(__name__)

# update_workflow_status 的 error 参数哨兵：None 表示清空错误，不传表示不修改
_ERROR_UNSET = object()


def create_workflow(
    user_id: int,
    name: str,
    requirement_text: str,
    knowledge_base_id: str | None = None,
    requirement_file_name: str | None = None,
) -> Workflow:
    db = create_session()
    try:
        workflow = Workflow(
            user_id=user_id,
            name=name,
            requirement_text=requirement_text,
            knowledge_base_id=knowledge_base_id,
            requirement_file_name=requirement_file_name,
        )
        db.add(workflow)
        db.commit()
        db.refresh(workflow)
        return workflow
    except Exception as e:
        db.rollback()
        logger.error("创建工作流失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def get_workflow(workflow_id: str) -> Workflow | None:
    db = create_session()
    try:
        return db.query(Workflow).filter(Workflow.id == workflow_id).first()
    finally:
        db.close()


def get_owned_workflow(user_id: int, workflow_id: str) -> Workflow | None:
    """获取属于指定用户的工作流（v1 工作流不做共享）"""
    db = create_session()
    try:
        return (
            db.query(Workflow)
            .filter(Workflow.id == workflow_id, Workflow.user_id == user_id)
            .first()
        )
    finally:
        db.close()


def rename_workflow(user_id: int, workflow_id: str, name: str) -> Workflow | None:
    """重命名工作流（owner-only）；任务不存在或不属于该用户时返回 None。

    只改名称：需求原文与全部产物（含 checkpoint）保持不动，因此运行中也可改名。
    """
    db = create_session()
    try:
        row = (
            db.query(Workflow)
            .filter(Workflow.id == workflow_id, Workflow.user_id == user_id)
            .first()
        )
        if row is None:
            return None
        row.name = name
        db.commit()
        db.refresh(row)
        return row
    except Exception as e:
        db.rollback()
        logger.error("重命名工作流失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def delete_workflow(user_id: int, workflow_id: str) -> bool:
    """删除工作流（owner-only），返回是否删掉了行。

    产物随 ORM 级联（Workflow.artifacts 的 delete-orphan）与数据库外键（ondelete CASCADE）
    一并删除；由该工作流发布出的测试用例集不删，其 source_workflow_id 靠外键 SET NULL 置空。
    运行中的互斥由端点用进程内运行注册表判断（本函数只管持久化）。
    """
    db = create_session()
    try:
        row = (
            db.query(Workflow)
            .filter(Workflow.id == workflow_id, Workflow.user_id == user_id)
            .first()
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        logger.info("测试工作流 %s 已删除", workflow_id)
        return True
    except Exception as e:
        db.rollback()
        logger.error("删除工作流失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def _like_pattern(keyword: str) -> str:
    """把关键词包成 LIKE 模式并转义通配符（用户输入 % 应按字面量匹配）"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_workflows(
    user_id: int,
    keyword: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[Workflow], int]:
    """按关键词分页查询任务，返回 (当前页任务, 命中总数)。

    keyword 同时匹配任务名与需求原文（两个引擎默认排序规则都不区分大小写，与列表页
    客户端筛选行为一致）；不传 limit 表示不分页（保持旧调用方的全量语义）。
    列表按 created_at 倒序，再用 id 兜底：同一秒创建的任务顺序才稳定。
    """
    db = create_session()
    try:
        query = db.query(Workflow).filter(Workflow.user_id == user_id)
        if keyword:
            pattern = _like_pattern(keyword)
            query = query.filter(
                or_(
                    Workflow.name.like(pattern, escape="\\"),
                    Workflow.requirement_text.like(pattern, escape="\\"),
                )
            )
        total = query.count()
        query = query.order_by(Workflow.created_at.desc(), Workflow.id.desc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return query.all(), total
    finally:
        db.close()


def list_workflows(user_id: int) -> list[Workflow]:
    """全量列出用户任务（列表页改用 search_workflows 分页后的兼容入口）"""
    return search_workflows(user_id)[0]


def update_workflow_status(
    workflow_id: str,
    status: str | None = None,
    current_step: str | None = None,
    error=_ERROR_UNSET,
) -> bool:
    """更新工作流状态；仅修改显式传入的字段（自建会话，供图节点经 to_thread 调用）"""
    db = create_session()
    try:
        row = db.query(Workflow).filter(Workflow.id == workflow_id).first()
        if not row:
            return False
        if status is not None:
            row.status = status
        if current_step is not None:
            row.current_step = current_step
        if error is not _ERROR_UNSET:
            row.error = error
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("更新工作流状态失败: %s", e, exc_info=True)
        return False
    finally:
        db.close()


def try_claim_workflow(
    workflow_id: str,
    expected_statuses: tuple[str, ...],
    status: str,
    current_step: str,
    error=_ERROR_UNSET,
) -> bool:
    """状态机乐观锁：仅当当前 status 在 expected_statuses 内才迁移，返回是否抢占成功。

    用于启动入口的互斥（UPDATE ... WHERE status IN ...），防止同一任务并发跑出
    两份图实例互相覆盖 checkpoint。
    """
    db = create_session()
    try:
        values = {"status": status, "current_step": current_step}
        if error is not _ERROR_UNSET:
            values["error"] = error
        updated = (
            db.query(Workflow)
            .filter(Workflow.id == workflow_id, Workflow.status.in_(expected_statuses))
            .update(values, synchronize_session=False)
        )
        db.commit()
        return updated > 0
    except Exception as e:
        db.rollback()
        logger.error("抢占工作流状态失败: %s", e, exc_info=True)
        return False
    finally:
        db.close()


def save_artifact(
    workflow_id: str,
    artifact_type: str,
    content: dict,
    parent_artifact_id: str | None = None,
) -> str:
    """保存工作流产物，版本号按类型自增，返回产物 id"""
    db = create_session()
    try:
        max_version = (
            db.query(func.max(Artifact.version))
            .filter(
                Artifact.workflow_id == workflow_id,
                Artifact.artifact_type == artifact_type,
            )
            .scalar()
        ) or 0
        artifact = Artifact(
            workflow_id=workflow_id,
            artifact_type=artifact_type,
            version=max_version + 1,
            parent_artifact_id=parent_artifact_id,
            content=json.dumps(content, ensure_ascii=False),
        )
        db.add(artifact)
        db.commit()
        logger.info(
            "工作流 %s 保存产物 %s v%s", workflow_id, artifact_type, artifact.version
        )
        return artifact.id
    except Exception as e:
        db.rollback()
        logger.error("保存工作流产物失败: %s", e, exc_info=True)
        raise
    finally:
        db.close()


def get_artifacts(workflow_id: str) -> list[Artifact]:
    db = create_session()
    try:
        return (
            db.query(Artifact)
            .filter(Artifact.workflow_id == workflow_id)
            .order_by(Artifact.created_at.asc(), Artifact.version.asc())
            .all()
        )
    finally:
        db.close()


def get_latest_artifact(workflow_id: str, artifact_type: str) -> Artifact | None:
    db = create_session()
    try:
        return (
            db.query(Artifact)
            .filter(
                Artifact.workflow_id == workflow_id,
                Artifact.artifact_type == artifact_type,
            )
            .order_by(Artifact.version.desc())
            .first()
        )
    finally:
        db.close()
