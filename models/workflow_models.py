import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

from models.database import Base


class Workflow(Base):
    """测试工作流表：一次从需求分析到测试用例生成的完整测试任务"""
    __tablename__ = "workflows"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False, default="新任务")
    requirement_text = Column(Text, nullable=False)
    knowledge_base_id = Column(String(36), ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    # created / analyzing / waiting_review / generating / completed / failed
    status = Column(String(20), nullable=False, default="created")
    current_step = Column(String(50), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    artifacts = relationship("Artifact", back_populates="workflow", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Workflow(id={self.id}, name='{self.name}', status='{self.status}')>"


class Artifact(Base):
    """工作流产物表：各节点产出的结构化结果，按版本追加，parent_artifact_id 记录修订来源"""
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("workflow_id", "artifact_type", "version", name="uq_artifact_workflow_type_version"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_id = Column(String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type = Column(String(50), nullable=False)  # requirement_analysis / test_case_set / coverage_report
    version = Column(Integer, nullable=False, default=1)
    parent_artifact_id = Column(String(36), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True)
    content = Column(Text, nullable=False)  # JSON 序列化的结构化产物
    created_at = Column(DateTime, default=func.now())

    workflow = relationship("Workflow", back_populates="artifacts")

    def __repr__(self):
        return f"<Artifact(id={self.id}, type='{self.artifact_type}', version={self.version})>"
