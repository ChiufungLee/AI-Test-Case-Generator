import pytest

from models.chat import Message
from utils import llm_handle


class DummyRequest:
    def __init__(self, disconnected=False):
        self._disconnected = disconnected

    async def is_disconnected(self):
        return self._disconnected


@pytest.mark.asyncio
async def test_disconnected_stream_does_not_persist_partial_response(monkeypatch, db_session, make_user, make_conversation):
    user = make_user("stream_user", "secret123")
    conversation = make_conversation(user.id)

    async def fake_call_llm_model(prompt, **kwargs):
        yield "partial"

    monkeypatch.setattr(llm_handle, "call_llm_model", fake_call_llm_model)

    chunks = []
    async for chunk in llm_handle.generate_response(
        DummyRequest(disconnected=True),
        "prompt",
        conversation.id,
        False,
        "hello",
    ):
        chunks.append(chunk)

    messages = db_session.query(Message).filter(Message.conversation_id == conversation.id).all()
    assert chunks == []
    assert messages == []


@pytest.mark.asyncio
async def test_completed_stream_persists_assistant_message(monkeypatch, db_session, make_user, make_conversation):
    user = make_user("stream_user_done", "secret123")
    conversation = make_conversation(user.id)

    async def fake_call_llm_model(prompt, **kwargs):
        for token in ["你", "好"]:
            yield token

    monkeypatch.setattr(llm_handle, "call_llm_model", fake_call_llm_model)

    chunks = []
    async for chunk in llm_handle.generate_response(
        DummyRequest(disconnected=False),
        "prompt",
        conversation.id,
        False,
        "hello",
    ):
        chunks.append(chunk)

    assert chunks[:-1] == ["data: {\"token\": \"你\"}\n\n", "data: {\"token\": \"好\"}\n\n"]
    assert chunks[-1] == "data: [DONE]\n\n"

    db_session.expire_all()
    messages = db_session.query(Message).filter(Message.conversation_id == conversation.id).all()
    assert [m.role for m in messages] == ["assistant"]
    assert messages[0].content == "你好"


class FakeEmptyStreamModel:
    """astream 返回 0 个 token 的假模型（连接正常闭合但无内容）"""

    def astream(self, llm_input, **kwargs):
        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


@pytest.mark.asyncio
async def test_call_llm_model_empty_stream_yields_fallback(monkeypatch):
    monkeypatch.setattr(llm_handle, "_get_cached_llm_model", lambda: FakeEmptyStreamModel())

    chunks = [chunk async for chunk in llm_handle.call_llm_model("hi")]

    assert chunks == ["[错误：模型未返回内容，请稍后重试]"]
