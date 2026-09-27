import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from main import create_app
from models.chat import Conversation, Message
from models.database import Base, create_session, init_db, reset_database
from models.knowledge_models import KnowledgeBase, KnowledgeFile
from models.user import User
from services.auth_service import AuthService
from utils.file_handle import get_document_processor, reset_document_processor_state
from utils.llm_handle import reset_llm_state
from utils.retriever import reset_retriever_state


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

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("UPLOAD_DIR", upload_dir.as_posix())
    monkeypatch.setenv("TEMP_UPLOAD_DIR", temp_upload_dir.as_posix())
    monkeypatch.setenv("RAG_DB_PATH", rag_dir.as_posix())
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    reset_database()
    reset_document_processor_state()
    reset_retriever_state()
    reset_llm_state()

    import models.user  # noqa: F401
    import models.chat  # noqa: F401
    import models.knowledge_models  # noqa: F401

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
            file_path=str(file_path.resolve()),
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
