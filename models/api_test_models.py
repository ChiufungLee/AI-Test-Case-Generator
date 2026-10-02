"""API 测试工作台模型：OpenAPI 规格资产、接口快照、接口用例（执行记录在 PR3 补充）"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    content = Column(Text, nullable=False)  # 原始文档全文；同步（URL 导入）时随远端刷新
    # URL 导入来源；手工粘贴导入为 NULL。「同步」按此地址重新拉取并刷新接口快照
    source_url = Column(String(500), nullable=True)
    # 登录态前置请求配置（D-025）：{"method","path","body","body_type","token_field"}，
    # 空串=未配置；执行时先发该请求收集 Cookie/Token 供本轮用例携带。凭据仅 owner 可见
    auth_config_json = Column(Text, nullable=False, default="")
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
    # 请求体媒体类型（D-024）：空串=无请求体；application/json、
    # application/x-www-form-urlencoded、multipart/form-data——执行层据此选择发送方式
    request_body_media_type = Column(String(100), nullable=False, default="")
    # 声明的响应码快照（如 {"200": "ok", "404": "not found"}），规则引擎据此推导正常用例预期状态
    responses_json = Column(Text, nullable=False, default="{}")

    api_spec = relationship("ApiSpec", back_populates="endpoints")

    def __repr__(self):
        return f"<ApiEndpoint(id={self.id}, method='{self.method}', path='{self.path}')>"


class ApiEndpointCase(Base):
    """接口用例：规则引擎/AI/手工生成的请求样例，执行阶段按 enabled 逐条发起请求"""
    __tablename__ = "api_endpoint_cases"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "name", name="uq_api_endpoint_case_name"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id = Column(String(36), ForeignKey("api_endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    # {"path": {...}, "query": {...}, "body": {...}, "headers": {...}}
    request_json = Column(Text, nullable=False, default="{}")
    expected_status = Column(Integer, nullable=False, default=200)
    # "rule_engine" / "ai" / "manual"
    source_type = Column(String(20), nullable=False, default="manual")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=func.now())

    endpoint = relationship("ApiEndpoint", backref="cases")

    def __repr__(self):
        return f"<ApiEndpointCase(id={self.id}, name='{self.name}', expected={self.expected_status})>"


class TestRun(Base):
    """一次接口用例批量执行（进程内 httpx 顺序执行，D-012）；结果逐条落 TestRunResult"""
    __tablename__ = "test_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    spec_id = Column(String(36), ForeignKey("api_specs.id", ondelete="SET NULL"), nullable=True, index=True)
    base_url = Column(String(500), nullable=False)
    # claim 时快照的 "METHOD /path" 去重列表（执行历史展示用）
    endpoints_json = Column(Text, nullable=False, default="[]")
    # running / completed / failed
    status = Column(String(20), nullable=False, default="running")
    # 失败原因（执行异常/登录态获取失败）；completed 为 NULL
    error = Column(Text, nullable=True)
    total = Column(Integer, nullable=False, default=0)
    passed = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    errored = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now())
    finished_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<TestRun(id={self.id}, status='{self.status}', passed={self.passed})>"


class TestRunResult(Base):
    """单条用例执行结果：请求/响应快照 + 断言结论（v1 仅状态码断言）"""
    __tablename__ = "test_run_results"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id = Column(String(36), ForeignKey("api_endpoints.id", ondelete="SET NULL"), nullable=True)
    case_name = Column(String(200), nullable=False)
    request_json = Column(Text, nullable=False, default="{}")
    response_json = Column(Text, nullable=False, default="{}")
    # passed / failed / error
    verdict = Column(String(20), nullable=False)
    expected_status = Column(Integer, nullable=True)
    actual_status = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<TestRunResult(run={self.run_id}, case='{self.case_name}', verdict='{self.verdict}')>"
