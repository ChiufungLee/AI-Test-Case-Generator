import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from main import create_app
from models.chat import Conversation, Message
from models.api_test_models import ApiEndpoint, ApiEndpointCase, ApiSpec
from models.database import Base, create_session, init_db
from models.knowledge_models import KnowledgeBase, KnowledgeFile
from models.test_asset_models import TestCaseSet, TestCaseSetVersion
from models.user import User
from models.workflow_models import Workflow
from services.auth_service import AuthService
from testing_state import (
    reset_database,
    reset_document_processor_state,
    reset_llm_state,
    reset_retriever_state,
    reset_workflow_state,
)
from utils.file_handle import get_document_processor


class DummyCollection:
    def __init__(self):
        self.items = []
        self.metadata = {}
        self.deleted_wheres = []

    def add(self, ids, documents, embeddings, metadatas):
        for item in zip(ids, documents, embeddings, metadatas):
            self.items.append(item)

    def delete(self, where=None):
        self.deleted_wheres.append(where)
        if where and "file_id" in where:
            self.items = [item for item in self.items if item[3].get("file_id") != where["file_id"]]

    def count(self):
        return len(self.items)

    def query(self, query_embeddings=None, n_results=3, include=None, **kwargs):
        docs = [item[1] for item in self.items[:n_results]]
        metas = [item[3] for item in self.items[:n_results]]
        return {"documents": [docs], "metadatas": [metas]}


class DummyChromaClient:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name, metadata=None):
        collection = self.collections.setdefault(name, DummyCollection())
        if metadata:
            collection.metadata = metadata
        return collection

    def get_collection(self, name):
        if name not in self.collections:
            raise KeyError(name)
        return self.collections[name]

    def delete_collection(self, name):
        self.collections.pop(name, None)


@pytest.fixture()
def test_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    temp_upload_dir = tmp_path / "temp_uploads"
    rag_dir = tmp_path / "chroma"
    checkpoint_db = tmp_path / "langgraph_checkpoints.db"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("UPLOAD_DIR", upload_dir.as_posix())
    monkeypatch.setenv("TEMP_UPLOAD_DIR", temp_upload_dir.as_posix())
    monkeypatch.setenv("RAG_DB_PATH", rag_dir.as_posix())
    monkeypatch.setenv("WORKFLOW_CHECKPOINT_DB_PATH", checkpoint_db.as_posix())
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    reset_database()
    reset_document_processor_state()
    reset_retriever_state()
    reset_llm_state()
    reset_workflow_state()

    import models.api_test_models  # noqa: F401
    import models.user  # noqa: F401
    import models.chat  # noqa: F401
    import models.knowledge_models  # noqa: F401
    import models.workflow_models  # noqa: F401
    import models.test_asset_models  # noqa: F401

    init_db()
    yield {
        "db_path": db_path,
        "upload_dir": upload_dir,
        "temp_upload_dir": temp_upload_dir,
        "rag_dir": rag_dir,
    }


class DummyEmbeddingResponse:
    def __init__(self):
        self.usage = type("Usage", (), {"total_tokens": 1})()
        self.data = [type("EmbeddingData", (), {"embedding": [0.1, 0.2, 0.3]})()]


class _DummyEmbeddingsAPI:
    async def create(self, **kwargs):
        return DummyEmbeddingResponse()


class DummyAsyncEmbeddingClient:
    def __init__(self):
        self.embeddings = _DummyEmbeddingsAPI()


@pytest.fixture()
def stub_external_dependencies(monkeypatch):
    dummy_chroma = DummyChromaClient()

    monkeypatch.setattr("utils.file_handle.get_chromadb_client", lambda: dummy_chroma)
    monkeypatch.setattr(
        "utils.file_handle.DocumentProcessor.embed",
        lambda self, text: [0.1, 0.2, 0.3],
    )
    monkeypatch.setattr(
        "utils.file_handle.DocumentProcessor.embed_batch",
        lambda self, texts, batch_size=10: [[0.1, 0.2, 0.3] for _ in texts],
    )
    monkeypatch.setattr(
        "utils.file_handle.DocumentProcessor.load_pdf",
        lambda self, file_path: [Document(page_content="pdf content", metadata={"source": file_path})],
    )
    monkeypatch.setattr(
        "utils.retriever.get_async_embedding_client",
        lambda: DummyAsyncEmbeddingClient(),
    )
    return dummy_chroma


@pytest.fixture()
def document_processor(stub_external_dependencies):
    return get_document_processor()


@pytest.fixture()
def app(test_env, stub_external_dependencies):
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db_session(test_env):
    db = create_session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def make_user(db_session):
    def _make_user(username: str, password: str, hashed: bool = True):
        stored_password = AuthService.hash_password(password) if hashed else password
        user = User(username=username, password=stored_password)
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    return _make_user


@pytest.fixture()
def logged_in_client(client):
    client.post(
        "/register",
        data={"username": "alice", "password": "secret123"},
        follow_redirects=False,
    )
    response = client.post(
        "/login",
        data={"username": "alice", "password": "secret123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


@pytest.fixture()
def make_conversation(db_session):
    def _make_conversation(user_id: int, title: str = "新对话", scenario: str = "test_case_generation", knowledge_base_id=None):
        conversation = Conversation(
            user_id=user_id,
            title=title,
            scenario=scenario,
            knowledge_base_id=knowledge_base_id,
        )
        db_session.add(conversation)
        db_session.commit()
        db_session.refresh(conversation)
        return conversation

    return _make_conversation


@pytest.fixture()
def make_message(db_session):
    def _make_message(conversation_id: str, role: str, content: str):
        message = Message(conversation_id=conversation_id, role=role, content=content)
        db_session.add(message)
        db_session.commit()
        db_session.refresh(message)
        return message

    return _make_message


@pytest.fixture()
def make_knowledge_base(db_session):
    def _make_kb(owner_user_id: int, name: str = "KB", description: str = "desc", collection_name: str | None = None):
        kb = KnowledgeBase(
            owner_user_id=owner_user_id,
            name=name,
            description=description,
            collection_name=collection_name or f"kb_{owner_user_id}_{name}",
        )
        db_session.add(kb)
        db_session.commit()
        db_session.refresh(kb)
        return kb

    return _make_kb


@pytest.fixture()
def make_knowledge_file(db_session, test_env):
    def _make_file(knowledge_base_id: str, filename: str = "sample.pdf", content: bytes = b"%PDF-1.4\n%stub\n"):
        upload_dir = Path(test_env["upload_dir"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / filename
        file_path.write_bytes(content)
        record = KnowledgeFile(
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            file_path=filename,
            file_size=len(content),
            file_type="pdf",
            status="completed",
            chunk_count=1,
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)
        return record

    return _make_file


@pytest.fixture()
def make_workflow(db_session):
    def _make_workflow(
        user_id: int,
        name: str = "测试工作流",
        requirement_text: str = "需求：新增手机号验证码登录，连续5次验证码错误后锁定30分钟",
        knowledge_base_id: str | None = None,
        status: str = "created",
    ):
        workflow = Workflow(
            user_id=user_id,
            name=name,
            requirement_text=requirement_text,
            knowledge_base_id=knowledge_base_id,
            status=status,
        )
        db_session.add(workflow)
        db_session.commit()
        db_session.refresh(workflow)
        return workflow

    return _make_workflow


@pytest.fixture()
def make_test_case_set(db_session):
    def _make_set(
        owner_user_id: int,
        name: str = "登录用例集",
        source_workflow_id: str | None = None,
        visibility: str = "private",
        current_version: int = 1,
        case_count: int = 0,
    ):
        asset = TestCaseSet(
            owner_user_id=owner_user_id,
            name=name,
            source_workflow_id=source_workflow_id,
            visibility=visibility,
            current_version=current_version,
            case_count=case_count,
        )
        db_session.add(asset)
        db_session.commit()
        db_session.refresh(asset)
        return asset

    return _make_set


@pytest.fixture()
def make_test_case_set_version(db_session):
    def _make_version(
        test_case_set_id: str,
        version: int,
        content: dict,
        source_type: str = "manual_edit",
        parent_version_id: str | None = None,
        source_version_id: str | None = None,
        source_artifact_id: str | None = None,
        note: str | None = None,
    ):
        row = TestCaseSetVersion(
            test_case_set_id=test_case_set_id,
            version=version,
            content=json.dumps(content, ensure_ascii=False),
            source_type=source_type,
            parent_version_id=parent_version_id,
            source_version_id=source_version_id,
            source_artifact_id=source_artifact_id,
            note=note,
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        # 同步资产的当前版本指针，保持与真实写入路径一致的资产状态
        asset = db_session.query(TestCaseSet).filter(TestCaseSet.id == test_case_set_id).first()
        if asset is not None and version > asset.current_version:
            asset.current_version = version
            db_session.commit()
            db_session.refresh(asset)
        return row

    return _make_version


_MINIMAL_SPEC_YAML = """openapi: 3.0.0
info:
  title: 示例 API
  version: 1.0.0
paths:
  /users:
    post:
      operationId: createUser
      summary: 创建用户
      responses:
        '201':
          description: created
"""


@pytest.fixture()
def make_api_spec(db_session):
    def _make_spec(
        owner_user_id: int,
        name: str = "示例 API",
        content: str | None = None,
        format: str = "yaml",
        visibility: str = "private",
        endpoint_count: int = 0,
    ):
        spec = ApiSpec(
            owner_user_id=owner_user_id,
            name=name,
            format=format,
            content=content if content is not None else _MINIMAL_SPEC_YAML,
            spec_title="示例 API",
            spec_version="1.0.0",
            endpoint_count=endpoint_count,
            visibility=visibility,
        )
        db_session.add(spec)
        db_session.commit()
        db_session.refresh(spec)
        return spec

    return _make_spec


@pytest.fixture()
def make_api_endpoint(db_session):
    def _make_endpoint(spec_id: str, method: str = "post", path: str = "/users", **kwargs):
        row = ApiEndpoint(
            spec_id=spec_id,
            method=method,
            path=path,
            operation_id=kwargs.get("operation_id", ""),
            summary=kwargs.get("summary", ""),
            parameters_json=kwargs.get("parameters_json", "[]"),
            request_body_json=kwargs.get("request_body_json", ""),
            responses_json=kwargs.get("responses_json", "{}"),
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        return row

    return _make_endpoint


@pytest.fixture()
def make_api_endpoint_case(db_session):
    def _make_case(
        endpoint_id: str,
        name: str = "正常请求",
        request_json: str = "{}",
        expected_status: int = 200,
        source_type: str = "rule_engine",
        enabled: bool = True,
    ):
        row = ApiEndpointCase(
            endpoint_id=endpoint_id,
            name=name,
            request_json=request_json,
            expected_status=expected_status,
            source_type=source_type,
            enabled=enabled,
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        return row

    return _make_case


class FakeStructuredLLM:
    """替换 workflows.nodes._invoke_structured 的桩实现。

    fail_times>0 时前 N 次调用抛异常，用于验证重试与失败落库路径；
    calls 记录实际调用次数。返回值与 _invoke_structured 一致：(结果, truncated)。
    """

    def __init__(self):
        self.calls = 0
        self.fail_times = 0
        self.truncated = False
        from schemas.workflow_schemas import (
            RequirementAnalysis,
            RequirementItem,
            RiskItem,
            TestCase,
            TestCaseSet,
        )

        self.analysis = RequirementAnalysis(
            summary="手机号验证码登录功能",
            scope=["登录功能"],
            functional_requirements=[
                RequirementItem(id="REQ-001", title="验证码登录", description="输入手机号与验证码登录"),
                RequirementItem(id="REQ-002", title="错误锁定", description="连续5次错误验证码后锁定30分钟"),
            ],
            business_rules=["连续5次验证码错误后锁定30分钟"],
            acceptance_criteria=["正确验证码可登录", "5次错误后账户锁定"],
            risks=[RiskItem(id="RISK-001", description="验证码可能被暴力尝试", level="medium")],
            assumptions=[],
        )
        self.cases = TestCaseSet(
            test_cases=[
                TestCase(
                    id="TC-AUTH-001",
                    title="正确验证码登录成功",
                    preconditions=["用户已注册"],
                    steps=["输入手机号", "输入正确验证码"],
                    expected_results=["登录成功"],
                    priority="P0",
                    automation="Auto",
                    requirement_refs=["REQ-001"],
                ),
                TestCase(
                    id="TC-AUTH-002",
                    title="连续5次错误验证码锁定账户",
                    preconditions=["用户已注册"],
                    steps=["输入手机号", "连续输入错误验证码5次"],
                    expected_results=["账户锁定30分钟"],
                    priority="P0",
                    automation="Manual",
                    requirement_refs=["REQ-002"],
                ),
            ]
        )
        # 非空时按次序弹出作为 TestCaseSet 返回（AI 修改的"校验失败重试"路径测试用）
        self.cases_sequence = []
        # AI 用例建议桩（openapi_business_cases_workflow 的结构化输出；None 时按未预期 schema 处理）
        self.case_proposals = None

    async def __call__(self, messages, schema, temperature):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("模拟结构化输出失败")
        from schemas.api_test_schemas import ApiCaseProposalSet
        from schemas.workflow_schemas import RequirementAnalysis, TestCaseSet

        if schema is RequirementAnalysis:
            return self.analysis, self.truncated
        if schema is TestCaseSet:
            if self.cases_sequence:
                return self.cases_sequence.pop(0), self.truncated
            return self.cases, self.truncated
        if schema is ApiCaseProposalSet:
            if self.case_proposals is None:
                raise AssertionError(f"未预期的 schema: {schema}")
            return self.case_proposals, self.truncated
        raise AssertionError(f"未预期的 schema: {schema}")


@pytest.fixture()
def stub_workflow_llm(monkeypatch):
    import workflows.nodes as workflow_nodes

    fake = FakeStructuredLLM()
    monkeypatch.setattr(workflow_nodes, "_invoke_structured", fake)
    return fake


def parse_sse_events(text: str) -> list[dict]:
    """把 SSE 响应体解析为事件 dict 列表（忽略 [DONE]）"""
    events = []
    for line in text.split("\n\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        data_str = line.replace("data: ", "").strip()
        if data_str == "[DONE]":
            continue
        events.append(json.loads(data_str))
    return events
