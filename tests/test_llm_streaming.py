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
async def test_call_llm_model_empty_stream_raises_generation_error(monkeypatch):
    monkeypatch.setattr(llm_handle, "_get_cached_llm_model", lambda: FakeEmptyStreamModel())

    with pytest.raises(llm_handle.LLMGenerationError) as exc_info:
        [chunk async for chunk in llm_handle.call_llm_model("hi")]

    # 用户提示为通用文案，不含异常细节
    assert exc_info.value.user_message == "模型未返回内容，请稍后重试"


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
async def test_call_llm_model_raises_after_retry_still_empty(monkeypatch):
    model = AlwaysEmptyContentModel()
    monkeypatch.setattr(llm_handle, "_get_cached_llm_model", lambda: model)

    with pytest.raises(llm_handle.LLMGenerationError):
        [chunk async for chunk in llm_handle.call_llm_model("hi")]

    assert model.calls == 2


class FailingModel:
    """astream 直接抛异常的假模型（模拟网络/接口错误）"""

    def __init__(self):
        self.calls = 0

    def astream(self, llm_input, **kwargs):
        self.calls += 1

        async def _gen():
            raise RuntimeError("connection refused to https://api.example.com/v1")
            yield  # pragma: no cover

        return _gen()


@pytest.mark.asyncio
async def test_call_llm_model_error_details_stay_in_logs(monkeypatch):
    """异常原文（可能含 URL 等）不进入生成器输出，只抛通用提示"""
    monkeypatch.setattr(llm_handle, "_get_cached_llm_model", lambda: FailingModel())

    with pytest.raises(llm_handle.LLMGenerationError) as exc_info:
        [chunk async for chunk in llm_handle.call_llm_model("hi")]

    assert exc_info.value.user_message == "生成失败，请稍后重试"
    assert "api.example.com" not in exc_info.value.user_message


@pytest.mark.asyncio
async def test_generate_response_emits_error_event_without_persisting_it(
    monkeypatch, db_session, make_user, make_conversation
):
    """生成失败：错误经独立 SSE 事件下发，不进入落库内容；已完成的部分照常保存"""
    user = make_user("stream_user_err", "secret123")
    conversation = make_conversation(user.id)

    async def fake_call_llm_model(prompt, **kwargs):
        yield "部分"
        raise llm_handle.LLMGenerationError("生成失败，请稍后重试")

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

    assert chunks[0] == "data: {\"token\": \"部分\"}\n\n"
    assert chunks[-1] == "data: {\"error\": \"生成失败，请稍后重试\"}\n\n"
    # 失败后没有 [DONE]
    assert "data: [DONE]\n\n" not in chunks

    db_session.expire_all()
    messages = db_session.query(Message).filter(Message.conversation_id == conversation.id).all()
    assert [m.role for m in messages] == ["assistant"]
    assert messages[0].content == "部分"


@pytest.mark.asyncio
async def test_generate_regenerate_response_error_keeps_old_message(
    monkeypatch, db_session, make_user, make_conversation
):
    """重新生成失败：下发错误事件，旧 AI 回复保持不变"""
    user = make_user("regen_user_err", "secret123")
    conversation = make_conversation(user.id)
    old_message = Message(conversation_id=conversation.id, role="assistant", content="旧回复")
    db_session.add(old_message)
    db_session.commit()

    async def fake_call_llm_model(prompt, **kwargs):
        raise llm_handle.LLMGenerationError("生成响应超时，请稍后重试")
        yield  # pragma: no cover

    monkeypatch.setattr(llm_handle, "call_llm_model", fake_call_llm_model)

    chunks = []
    async for chunk in llm_handle.generate_regenerate_response(
        DummyRequest(disconnected=False),
        "prompt",
        conversation.id,
        old_message.id,
    ):
        chunks.append(chunk)

    assert chunks == ["data: {\"error\": \"生成响应超时，请稍后重试\"}\n\n"]

    db_session.expire_all()
    messages = db_session.query(Message).filter(Message.conversation_id == conversation.id).all()
    assert [m.content for m in messages] == ["旧回复"]


def test_llm_enable_thinking_env_parsing(monkeypatch):
    from config import get_llm_config

    get_llm_config.cache_clear()
    monkeypatch.setenv("LLM_ENABLE_THINKING", "true")
    assert get_llm_config().enable_thinking is True

    get_llm_config.cache_clear()
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    assert get_llm_config().enable_thinking is False
    get_llm_config.cache_clear()


def _capture_model_init(monkeypatch) -> dict:
    captured = {}

    def fake_init_chat_model(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(llm_handle, "init_chat_model", fake_init_chat_model)
    return captured


def test_llm_model_init_disables_thinking_by_default(monkeypatch):
    captured = _capture_model_init(monkeypatch)
    llm_handle._get_cached_llm_model.cache_clear()
    llm_handle._get_cached_llm_model()
    llm_handle._get_cached_llm_model.cache_clear()

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_llm_model_init_keeps_thinking_when_enabled(monkeypatch):
    from config import get_llm_config

    captured = _capture_model_init(monkeypatch)
    monkeypatch.setenv("LLM_ENABLE_THINKING", "true")
    get_llm_config.cache_clear()
    llm_handle._get_cached_llm_model.cache_clear()
    llm_handle._get_cached_llm_model()
    llm_handle._get_cached_llm_model.cache_clear()
    get_llm_config.cache_clear()

    assert "extra_body" not in captured
