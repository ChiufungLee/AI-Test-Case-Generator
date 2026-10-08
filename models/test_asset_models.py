import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

from models.database import Base, LongText


class TestCaseSet(Base):
    """测试用例集资产表：由工作流 test_case_set 产物发布而来，可长期编辑与版本化"""
    __tablename__ = "test_case_sets"
    __table_args__ = (
        # 一个工作流至多发布一个资产（D-007/D-013）；唯一约束允许多个 NULL，
        # 工作流删除置空后不阻碍其他资产
        UniqueConstraint("source_workflow_id", name="uq_test_case_set_source_workflow"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    source_workflow_id = Column(String(36), ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True)
    # "private" / "shared"
    visibility = Column(String(20), nullable=False, default="private")
    # 冗余当前版本号，兼作编辑乐观锁基准（UPDATE ... WHERE current_version = base）
    current_version = Column(Integer, nullable=False, default=0)
    # 冗余用例数，供列表页展示
    case_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    versions = relationship("TestCaseSetVersion", back_populates="test_case_set", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<TestCaseSet(id={self.id}, name='{self.name}', v={self.current_version})>"


class TestCaseSetVersion(Base):
    """用例集版本表：追加式版本，content 与 Artifact 的 test_case_set 产物同构"""
    __tablename__ = "test_case_set_versions"
    __table_args__ = (
        UniqueConstraint("test_case_set_id", "version", name="uq_test_case_set_version"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    test_case_set_id = Column(String(36), ForeignKey("test_case_sets.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    # 产生基础：本版本在哪个版本之上产生（恒为乐观锁通过的 base_version 对应行，
    # 由服务层内部推导，调用方不可指定——版本链严格线性，D-014）
    parent_version_id = Column(String(36), ForeignKey("test_case_set_versions.id", ondelete="SET NULL"), nullable=True)
    # 内容来源：rollback 时记录被复制的历史版本行（D-014）
    source_version_id = Column(String(36), ForeignKey("test_case_set_versions.id", ondelete="SET NULL"), nullable=True)
    content = Column(LongText, nullable=False)  # JSON: {"test_cases": [...]}
    # publish / manual_edit / rollback / ai_edit（ai_edit 为 1.5 期预留）
    source_type = Column(String(20), nullable=False, default="manual_edit")
    source_artifact_id = Column(String(36), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True)
    note = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=func.now())

    test_case_set = relationship("TestCaseSet", back_populates="versions")

    def __repr__(self):
        return f"<TestCaseSetVersion(id={self.id}, set={self.test_case_set_id}, v={self.version})>"
