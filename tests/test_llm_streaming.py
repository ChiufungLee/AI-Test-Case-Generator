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


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class ReasoningOnlyOnceModel:
    """首次 astream 只产出空 content 块（模拟思考模型 reasoning 耗尽预算），重试后正常输出"""

    def __init__(self):
        self.calls = []

    def astream(self, llm_input, **kwargs):
        self.calls.append(kwargs)

        async def _gen():
            if len(self.calls) == 1:
                yield _FakeResponse("")
            else:
                yield _FakeResponse("澄清")
                yield _FakeResponse("结果")

        return _gen()


class AlwaysEmptyContentModel:
    """两次 astream 都只产出空 content 块的假模型"""

    def __init__(self):
        self.calls = 0

    def astream(self, llm_input, **kwargs):
        self.calls += 1

        async def _gen():
            return
            yield  # pragma: no cover

        return _gen()


@pytest.mark.asyncio
async def test_call_llm_model_retries_with_larger_budget_when_content_empty(monkeypatch):
    """思考模型 reasoning 耗尽输出预算导致正文为空时，应放大 max_tokens 自动重试"""
    model = ReasoningOnlyOnceModel()
    monkeypatch.setattr(llm_handle, "_get_cached_llm_model", lambda: model)

    chunks = [chunk async for chunk in llm_handle.call_llm_model("hi")]

    assert "".join(chunks) == "澄清结果"
    assert len(model.calls) == 2
    assert "max_tokens" not in model.calls[0]
    assert model.calls[1]["max_tokens"] == llm_handle._retry_max_tokens()
    assert model.calls[1]["max_tokens"] >= llm_handle.EMPTY_STREAM_RETRY_MAX_TOKENS


@pytest.mark.asyncio
async def test_call_llm_model_yields_fallback_after_retry_still_empty(monkeypatch):
    model = AlwaysEmptyContentModel()
    monkeypatch.setattr(llm_handle, "_get_cached_llm_model", lambda: model)

    chunks = [chunk async for chunk in llm_handle.call_llm_model("hi")]

    assert chunks == ["[错误：模型未返回内容，请稍后重试]"]
    assert model.calls == 2
