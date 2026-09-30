import pytest

from models.chat import Message
from models.knowledge_models import KnowledgeFile
from models.user import User
from services import knowledge_service
from utils import llm_handle


@pytest.fixture()
def fake_llm(monkeypatch):
    """拦截 LLM 调用，记录收到的 prompt 并返回固定 token 流"""
    captured = {}

    async def fake_call_llm_model(prompt, **kwargs):
        captured["prompt"] = prompt
        yield "好的"

    monkeypatch.setattr(llm_handle, "call_llm_model", fake_call_llm_model)
    return captured


def _alice_id(db_session):
    return db_session.query(User).filter(User.username == "alice").first().id


def _chat_data(conversation, kb_id=None):
    data = {
        "message": "总结这份文档",
        "scenario": "product_manual",
        "conversation_id": conversation.id,
    }
    if kb_id:
        data["knowledge_base_id"] = kb_id
    return data


def test_chat_attachment_into_knowledge_base(
    logged_in_client, make_user, make_knowledge_base, make_conversation,
    document_processor, db_session, fake_llm,
):
    alice = _alice_id(db_session)
    kb = make_knowledge_base(alice, name="attach kb", collection_name="attach_collection")
    conversation = make_conversation(alice, title="att", scenario="product_manual", knowledge_base_id=kb.id)

    response = logged_in_client.post(
        "/api/chat",
        data=_chat_data(conversation, kb_id=kb.id),
        files={"file": ("demo.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )

    assert response.status_code == 200
    body = response.text
    assert '"token"' in body
    assert "[DONE]" in body

    # 文档已登记并完成向量化
    db_session.expire_all()
    record = db_session.query(KnowledgeFile).filter(KnowledgeFile.knowledge_base_id == kb.id).first()
    assert record is not None
    assert record.status == "completed"
    assert record.filename == "demo.pdf"
    assert record.chunk_count == 1

    # 向量写入知识库集合，检索上下文包含文档内容
    collection = document_processor.chromadb_client.get_collection("attach_collection")
    assert collection.count() == 1

    # 提示词使用知识库检索文案，且上下文包含文档内容
    human_content = fake_llm["prompt"][-1].content
    assert "pdf content" in human_content
    assert f"从「{kb.name}」检索到" in human_content

    # 用户消息正文保持纯提问文本，附件名存独立字段
    messages = db_session.query(Message).filter(Message.conversation_id == conversation.id).all()
    assert messages[0].content == "总结这份文档"
    assert messages[0].attachment_name == "demo.pdf"


def test_chat_attachment_to_shared_kb_rejected_for_non_owner(
    client, make_user, make_knowledge_base, make_conversation, db_session, fake_llm,
):
    owner = make_user("attach_shared_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="shared attach kb", collection_name="shared_attach_col")
    kb.visibility = "shared"
    db_session.commit()

    intruder = make_user("attach_intruder", "secret123")
    conversation = make_conversation(intruder.id, title="att2", scenario="product_manual", knowledge_base_id=kb.id)

    client.post("/login", data={"username": "attach_intruder", "password": "secret123"}, follow_redirects=False)

    response = client.post(
        "/api/chat",
        data=_chat_data(conversation, kb_id=kb.id),
        files={"file": ("demo.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "共享知识库仅属主可附带文档入库"
    # 未产生文件记录
    db_session.expire_all()
    assert db_session.query(KnowledgeFile).count() == 0


def test_plain_chat_attachment_direct_analysis(
    logged_in_client, make_user, make_conversation, db_session, fake_llm, document_processor,
):
    alice = _alice_id(db_session)
    conversation = make_conversation(alice, title="att3", scenario="product_manual")

    response = logged_in_client.post(
        "/api/chat",
        data=_chat_data(conversation),
        files={"file": ("plain.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )

    assert response.status_code == 200

    # 普通路径不产生文件记录与向量
    db_session.expire_all()
    assert db_session.query(KnowledgeFile).count() == 0

    # 提示词使用上传文档引导语，且包含文档文本
    human_content = fake_llm["prompt"][-1].content
    assert "pdf content" in human_content
    assert "用户上传的文档《plain.pdf》" in human_content

    # 用户消息正文保持纯提问文本，附件名与文档正文存独立字段
    messages = db_session.query(Message).filter(Message.conversation_id == conversation.id).all()
    assert messages[0].content == "总结这份文档"
    assert messages[0].attachment_name == "plain.pdf"
    assert messages[0].attachment_text and "pdf content" in messages[0].attachment_text


def test_regenerate_plain_attachment_reuses_stored_context(
    logged_in_client, make_user, make_conversation, db_session, fake_llm, document_processor,
):
    """普通对话附件：重新生成时用落库的附件正文恢复上下文，提问不带附件前缀"""
    alice = _alice_id(db_session)
    conversation = make_conversation(alice, title="att6", scenario="product_manual")

    # 首次提问带附件
    response = logged_in_client.post(
        "/api/chat",
        data=_chat_data(conversation),
        files={"file": ("plain.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )
    assert response.status_code == 200

    # 人工补一条 AI 回复，使 regenerate 有旧回复可删
    db_session.add(Message(conversation_id=conversation.id, role="assistant", content="旧的回复"))
    db_session.commit()

    response = logged_in_client.post(
        "/api/chat/regenerate",
        json={"conversation_id": conversation.id},
    )
    assert response.status_code == 200
    assert "[DONE]" in response.text

    # 提问是干净文本，上下文来自 attachment_text
    human_content = fake_llm["prompt"][-1].content
    assert "总结这份文档" in human_content
    assert "【附件" not in human_content
    assert "pdf content" in human_content
    assert "用户上传的文档《plain.pdf》" in human_content

    # 旧 AI 回复被替换，历史中不再残留附件前缀
    db_session.expire_all()
    contents = [m.content for m in db_session.query(Message).filter(Message.conversation_id == conversation.id).all()]
    assert "旧的回复" not in contents
    assert "总结这份文档" in contents


def test_regenerate_kb_attachment_retrieves_with_clean_question(
    logged_in_client, make_user, make_knowledge_base, make_conversation, db_session, fake_llm, document_processor,
):
    """知识库附件：重新生成时以干净提问做 RAG 检索，前缀不污染查询"""
    alice = _alice_id(db_session)
    kb = make_knowledge_base(alice, name="regen kb", collection_name="regen_collection")
    conversation = make_conversation(alice, title="att7", scenario="product_manual", knowledge_base_id=kb.id)

    response = logged_in_client.post(
        "/api/chat",
        data=_chat_data(conversation, kb_id=kb.id),
        files={"file": ("demo.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )
    assert response.status_code == 200

    db_session.add(Message(conversation_id=conversation.id, role="assistant", content="旧的回复"))
    db_session.commit()

    response = logged_in_client.post(
        "/api/chat/regenerate",
        json={"conversation_id": conversation.id},
    )
    assert response.status_code == 200

    # 检索查询不含附件前缀，上下文仍来自知识库
    human_content = fake_llm["prompt"][-1].content
    assert "总结这份文档" in human_content
    assert "【附件" not in human_content
    assert "pdf content" in human_content
    assert f"从「{kb.name}」检索到" in human_content


def test_chat_attachment_rejects_non_pdf(
    logged_in_client, make_user, make_conversation, db_session, fake_llm,
):
    alice = _alice_id(db_session)
    conversation = make_conversation(alice, title="att4", scenario="product_manual")

    response = logged_in_client.post(
        "/api/chat",
        data=_chat_data(conversation),
        files={"file": ("demo.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "仅支持上传 PDF 文件"


def test_plain_chat_attachment_truncated(
    logged_in_client, make_user, make_conversation, db_session, fake_llm, monkeypatch,
):
    async def fake_extract(file):
        return "长" * (30_000 + 5_000)

    monkeypatch.setattr(knowledge_service, "extract_pdf_text", fake_extract)

    alice = _alice_id(db_session)
    conversation = make_conversation(alice, title="att5", scenario="product_manual")

    response = logged_in_client.post(
        "/api/chat",
        data=_chat_data(conversation),
        files={"file": ("big.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )

    assert response.status_code == 200
    human_content = fake_llm["prompt"][-1].content
    assert "仅展示前 30000 字符" in human_content
