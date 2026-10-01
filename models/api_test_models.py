"""API 测试工作台模型：OpenAPI 规格资产、接口快照（用例与执行记录在后续 PR 补充）"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from models.database import Base


class ApiSpec(Base):
    """OpenAPI 规格资产：导入的原始文档 + 解析统计（可见性语义对齐 KnowledgeBase/TestCaseSet）"""
    __tablename__ = "api_specs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    # "yaml" / "json"
    format = Column(String(10), nullable=False, default="yaml")
    content = Column(Text, nullable=False)  # 原始文档全文，导入后不再改动（重导入=新资产）
    spec_title = Column(String(200), default="")   # info.title 快照
    spec_version = Column(String(50), default="")  # info.version 快照
    endpoint_count = Column(Integer, default=0)
    # "private" / "shared"
    visibility = Column(String(20), nullable=False, default="private")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    endpoints = relationship("ApiEndpoint", back_populates="api_spec", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ApiSpec(id={self.id}, name='{self.name}', endpoints={self.endpoint_count})>"


class ApiEndpoint(Base):
    """接口快照：导入时从 paths 展平的操作（path × method 一行），schema 已做局部 $ref 解引用"""
    __tablename__ = "api_endpoints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    spec_id = Column(String(36), ForeignKey("api_specs.id", ondelete="CASCADE"), nullable=False, index=True)
    method = Column(String(10), nullable=False)  # 小写 http method（get/post/put/patch/delete/...）
    path = Column(String(500), nullable=False)
    operation_id = Column(String(200), default="")
    summary = Column(String(500), default="")
    # 该操作的 parameters 数组 JSON（含 path/query 参数，path 级与操作级同名去重合并）
    parameters_json = Column(Text, nullable=False, default="[]")
    # requestBody 的 JSON Schema 快照（已解引用；无请求体为空串）
    request_body_json = Column(Text, nullable=False, default="")

    api_spec = relationship("ApiSpec", back_populates="endpoints")

    def __repr__(self):
        return f"<ApiEndpoint(id={self.id}, method='{self.method}', path='{self.path}')>"
