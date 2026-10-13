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
    reset_document_pool_state,
    reset_document_processor_state,
    reset_llm_state,
    reset_login_rate_limit_state,
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
    reset_login_rate_limit_state()
    reset_document_pool_state()

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
def make_layout_pdf():
    """生成一张"内容流按列写入 + 45° 斜排水印"的 PDF。

    复现真实文档的版面特征：Word/网页导出的 PDF 常把表格按"列"顺序写入内容流
    （pypdf 顺序读出时表格被拆成一列一行），水印则是斜排文字。
    单元格值给成 list 即写成一个"单元格内多行"（行内换行）的单元格。
    只写 ASCII 文本（用标准 14 号字体，无需内嵌字体），几何关系与中文文档一致。
    """

    def _make_layout_pdf(
        path,
        rows,
        columns_x=(50, 200, 400),
        row_top=700,
        row_height=20,
        watermark="CONFIDENTIAL",
    ):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        page = writer.pages[0]

        operations = ["BT", "/F1 12 Tf"]
        # 每行占多高由该行最长的单元格决定（单元格内换行不同行）
        row_tops = []
        y = row_top
        for row in rows:
            line_count = max(
                [len(value) if isinstance(value, list) else 1 for value in row] or [1]
            )
            row_tops.append(y)
            y -= line_count * row_height

        # 按列写入：第一列所有行、第二列所有行、第三列所有行……
        for column_index, x in enumerate(columns_x):
            for row_index, row in enumerate(rows):
                if column_index >= len(row):
                    continue
                value = row[column_index]
                lines = value if isinstance(value, list) else [value]
                for line_index, line in enumerate(lines):
                    # 单元格内的换行用更小的行距，与"新的一行"区分
                    line_y = row_tops[row_index] - line_index * (row_height * 0.8)
                    operations.append(f"1 0 0 1 {x} {line_y} Tm ({line}) Tj")
        # 斜排水印：字形矩阵含旋转分量
        operations += [
            "/F1 28 Tf",
            "0.707 0.707 -0.707 0.707 120 300 Tm",
            f"({watermark}) Tj",
            "ET",
        ]

        stream = DecodedStreamObject()
        stream.set_data("\n".join(operations).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )

        with open(path, "wb") as handle:
            writer.write(handle)
        return Path(path)

    return _make_layout_pdf


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
        **kwargs,
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
            source_url=kwargs.get("source_url"),
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
            request_body_media_type=kwargs.get("request_body_media_type", ""),
            responses_json=kwargs.get("responses_json", "{}"),
            response_schemas_json=kwargs.get("response_schemas_json", ""),
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
        assertions_json: str = "[]",
        source_type: str = "rule_engine",
        enabled: bool = True,
    ):
        row = ApiEndpointCase(
            endpoint_id=endpoint_id,
            name=name,
            request_json=request_json,
            expected_status=expected_status,
            assertions_json=assertions_json,
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
            TestPoint,
            TestPointSet,
        )

        self.analysis = RequirementAnalysis(
            summary="手机号验证码登录功能",
            scope=["登录功能"],
            roles=["普通用户"],
            functional_requirements=[
                RequirementItem(id="REQ-001", title="验证码登录", description="输入手机号与验证码登录"),
                RequirementItem(id="REQ-002", title="错误锁定", description="连续5次错误验证码后锁定30分钟"),
            ],
            business_rules=["连续5次验证码错误后锁定30分钟"],
            constraints=[],
            preconditions=["用户已注册"],
            acceptance_criteria=["正确验证码可登录", "5次错误后账户锁定"],
            risks=[RiskItem(id="RISK-001", description="验证码可能被暴力尝试", level="medium")],
            assumptions=[],
        )
        self.test_points = TestPointSet(
            test_points=[
                TestPoint(
                    id="TP-001",
                    title="验证码登录成功",
                    description="输入正确验证码可登录",
                    requirement_refs=["REQ-001"],
                    dimension="function",
                    risk_level="medium",
                    priority="P0",
                    rationale="核心登录路径",
                ),
                TestPoint(
                    id="TP-002",
                    title="连续5次错误验证码后锁定",
                    description="错误次数达到上限后的锁定行为",
                    requirement_refs=["REQ-002"],
                    dimension="boundary",
                    risk_level="high",
                    priority="P0",
                    rationale="安全与业务规则的边界",
                ),
            ],
            coverage_target="覆盖登录与锁定功能",
            notes=[],
        )
        # 非空时按次序弹出作为 TestPointSet 返回（测试点设计阶段的校验/重试路径测试用）
        self.test_point_sets_sequence = []
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
        from schemas.workflow_schemas import RequirementAnalysis, TestCaseSet, TestPointSet

        if schema is RequirementAnalysis:
            return self.analysis, self.truncated
        if schema is TestPointSet:
            if self.test_point_sets_sequence:
                return self.test_point_sets_sequence.pop(0), self.truncated
            return self.test_points, self.truncated
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
