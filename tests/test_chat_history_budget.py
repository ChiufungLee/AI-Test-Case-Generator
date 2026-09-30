"""历史消息 token 预算裁剪测试"""
from services.chat_service import ChatService


def test_estimate_text_tokens_cjk_vs_ascii():
    assert ChatService.estimate_text_tokens("") == 0
    # CJK 约 1 字符/token
    assert ChatService.estimate_text_tokens("一二三四五") >= 5
    # ASCII 约 4 字符/token
    assert ChatService.estimate_text_tokens("abcdefgh") <= 5


def test_history_trimmed_by_token_budget(db_session, make_user, make_conversation, make_message):
    user = make_user("budget_user", "secret123")
    conversation = make_conversation(user.id)

    big_table = "|" * 5000
    make_message(conversation.id, "user", "表格问题1")
    make_message(conversation.id, "assistant", big_table)
    make_message(conversation.id, "user", "表格问题2")
    make_message(conversation.id, "assistant", big_table)
    make_message(conversation.id, "user", "最新追问")

    history = ChatService.get_conversation_history_messages(
        conversation.id, db_session, limit=10, max_tokens=100
    )

    contents = [m.content for m in history]
    assert contents[-1] == "最新追问"
    assert big_table not in contents
    # 除最新一条外，其余保留的消息不超预算
    older_cost = sum(ChatService.estimate_text_tokens(c) for c in contents[:-1])
    assert older_cost <= 100


def test_history_budget_always_keeps_newest_message(db_session, make_user, make_conversation, make_message):
    user = make_user("budget_user2", "secret123")
    conversation = make_conversation(user.id)

    make_message(conversation.id, "user", "问")
    make_message(conversation.id, "assistant", "答" * 3000)

    history = ChatService.get_conversation_history_messages(
        conversation.id, db_session, limit=10, max_tokens=10
    )

    # 最新一条即使单独超预算也保留，避免丢掉当前话题
    assert [m.content for m in history] == ["答" * 3000]


def test_history_without_budget_keeps_all_within_limit(db_session, make_user, make_conversation, make_message):
    user = make_user("budget_user3", "secret123")
    conversation = make_conversation(user.id)

    make_message(conversation.id, "user", "问1")
    make_message(conversation.id, "assistant", "答1")

    history = ChatService.get_conversation_history_messages(conversation.id, db_session, limit=10)

    assert [m.content for m in history] == ["问1", "答1"]
