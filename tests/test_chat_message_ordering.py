from datetime import datetime

import pytest

from models.chat import Message
from services.chat_service import ChatService


@pytest.mark.asyncio
async def test_identical_timestamp_messages_order_by_id(db_session, make_user, make_conversation):
    """MySQL DateTime 秒级精度下同秒落库的消息：按自增 id 排序，结果稳定"""
    user = make_user("order_user", "secret123")
    conversation = make_conversation(user.id)

    same_ts = datetime(2026, 1, 1, 12, 0, 0)
    first = Message(conversation_id=conversation.id, role="user", content="第一问", timestamp=same_ts)
    db_session.add(first)
    db_session.commit()
    second = Message(conversation_id=conversation.id, role="assistant", content="第一答", timestamp=same_ts)
    db_session.add(second)
    db_session.commit()
    assert first.timestamp == second.timestamp

    # 最新消息取 id 更大者
    last_user = await ChatService.get_last_user_message(conversation.id, db_session)
    last_ai = await ChatService.get_last_ai_message(conversation.id, db_session)
    assert last_user.id == first.id
    assert last_ai.id == second.id

    # 历史按 id 升序，即落库顺序
    history = await ChatService.get_conversation_history_messages(conversation.id, db_session, limit=10)
    assert [m.content for m in history] == ["第一问", "第一答"]

    # 会话详情同样按 id 升序
    detail = await ChatService.get_conversation_message(user.id, conversation.id, db_session)
    assert [m.content for m in detail] == ["第一问", "第一答"]
