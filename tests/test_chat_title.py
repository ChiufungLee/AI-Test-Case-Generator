"""对话标题生成回归测试。

背景：deepseek-v4 系思考模型的 reasoning token 计入 max_tokens，预算过小会把
正文清空，导致新对话标题永远退化为用户消息截断兜底。
"""
import json

import pytest

from models.chat import Conversation
from models.database import create_session


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


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeChatModel:
    """astream 供聊天流式使用；ainvoke 供标题生成使用并记录调用参数"""

    def __init__(self, title_content: str):
        self.title_content = title_content
        self.ainvoke_kwargs = []

    async def astream(self, messages, **kwargs):
        for tok in ("你好", "，这是回复。"):
            yield _FakeResponse(tok)

    async def ainvoke(self, messages, **kwargs):
        self.ainvoke_kwargs.append(kwargs)
        return _FakeResponse(self.title_content)


@pytest.fixture()
def fake_llm_factory(monkeypatch):
    from utils import llm_handle

    def _install(title_content: str) -> FakeChatModel:
        model = FakeChatModel(title_content)
        monkeypatch.setattr(llm_handle, "_get_cached_llm_model", lambda: model)
        return model

    return _install


def _create_conversation(client) -> str:
    resp = client.post("/api/conversation/new", data={"scenario": "requirement_clarification"})
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


def _send_first_message(client, conversation_id: str, message: str):
    return client.post(
        "/api/chat",
        data={
            "message": message,
            "scenario": "requirement_clarification",
            "conversation_id": conversation_id,
        },
    )


def _fresh_conversation(conversation_id: str):
    db = create_session()
    try:
        return db.query(Conversation).filter(Conversation.id == conversation_id).first()
    finally:
        db.close()


def test_new_conversation_title_generated_and_persisted(logged_in_client, fake_llm_factory):
    client = logged_in_client
    fake_llm_factory("手机号验证码登录需求澄清")
    conversation_id = _create_conversation(client)

    resp = _send_first_message(client, conversation_id, "帮我做一个登录功能，需要支持验证码")

    assert resp.status_code == 200
    title_events = [
        e["conversation_title"] for e in parse_sse_events(resp.text) if "conversation_title" in e
    ]
    assert title_events == ["手机号验证码登录需求澄清"]
    assert _fresh_conversation(conversation_id).title == "手机号验证码登录需求澄清"


def test_title_call_budget_not_starved_by_thinking_models(logged_in_client, fake_llm_factory):
    """标题调用的 max_tokens 必须为思考模型的 reasoning 留出空间，不能退回过小预算"""
    client = logged_in_client
    model = fake_llm_factory("手机号验证码登录需求澄清")
    conversation_id = _create_conversation(client)

    _send_first_message(client, conversation_id, "帮我做一个登录功能")

    assert model.ainvoke_kwargs, "新对话的首条消息应触发标题生成"
    for kwargs in model.ainvoke_kwargs:
        assert kwargs["max_tokens"] >= 200, (
            f"max_tokens={kwargs['max_tokens']} 不足以容纳思考模型的 reasoning + 正文"
        )


def test_empty_title_content_falls_back_to_truncated_message(logged_in_client, fake_llm_factory):
    """预算耗尽返回空正文时，标题应优雅降级为截断消息而非空白"""
    client = logged_in_client
    fake_llm_factory("")
    conversation_id = _create_conversation(client)

    message = "我们要做一个用户登录功能，支持手机号验证码登录，请帮我澄清这个需求"
    resp = _send_first_message(client, conversation_id, message)

    assert resp.status_code == 200
    title_events = [
        e["conversation_title"] for e in parse_sse_events(resp.text) if "conversation_title" in e
    ]
    fallback = message[:20] + "..."
    assert title_events == [fallback]
    assert _fresh_conversation(conversation_id).title == fallback
