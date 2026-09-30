import json
import logging

from sqlalchemy import func
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
) -> Workflow:
    db = create_session()
    try:
        workflow = Workflow(
            user_id=user_id,
            name=name,
            requirement_text=requirement_text,
            knowledge_base_id=knowledge_base_id,
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


def list_workflows(user_id: int) -> list[Workflow]:
    db = create_session()
    try:
        return (
            db.query(Workflow)
            .filter(Workflow.user_id == user_id)
            .order_by(Workflow.created_at.desc())
            .all()
        )
    finally:
        db.close()


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
