def test_user_cannot_read_another_users_conversation(client, make_user, make_conversation, make_message):
    owner = make_user("owner", "secret123")
    intruder = make_user("intruder", "secret123")
    conversation = make_conversation(owner.id, title="owner chat")
    make_message(conversation.id, "user", "hello")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)
    response = client.get(f"/api/conversation/{conversation.id}")

    assert response.status_code == 404



def test_user_cannot_rename_another_users_conversation(client, db_session, make_user, make_conversation):
    owner = make_user("owner_rename", "secret123")
    intruder = make_user("intruder_rename", "secret123")
    conversation = make_conversation(owner.id, title="before")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)
    response = client.post(f"/api/conversation/{conversation.id}/rename", json={"title": "after"})

    assert response.status_code == 404
    db_session.expire_all()
    refreshed = db_session.get(type(conversation), conversation.id)
    assert refreshed.title == "before"



def test_user_cannot_delete_another_users_conversation(client, db_session, make_user, make_conversation):
    owner = make_user("owner_delete", "secret123")
    intruder = make_user("intruder_delete", "secret123")
    conversation = make_conversation(owner.id, title="keep")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)
    response = client.delete(f"/api/conversation/{conversation.id}")

    assert response.status_code == 404
    db_session.expire_all()
    assert db_session.get(type(conversation), conversation.id) is not None



def test_user_cannot_export_another_users_conversation(client, make_user, make_conversation, make_message):
    owner = make_user("owner_export", "secret123")
    intruder = make_user("intruder_export", "secret123")
    conversation = make_conversation(owner.id, title="export")
    make_message(conversation.id, "assistant", "| col |\n| --- |\n| value |")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)
    response = client.get(f"/api/export/testcases?conversation_id={conversation.id}")

    assert response.status_code == 404



from models.chat import Conversation
from models.user import User
from prompts.prompts import get_prompt


def test_new_conversation_drops_other_users_knowledge_base(client, db_session, make_user, make_knowledge_base):
    owner = make_user("kb_owner_chat", "secret123")
    intruder = make_user("kb_intruder_chat", "secret123")
    kb = make_knowledge_base(owner.id, name="owner kb for chat")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)
    response = client.post(
        "/api/conversation/new",
        data={"scenario": "test_case_generation", "knowledge_base_id": kb.id},
    )

    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    db_session.expire_all()
    conversation = db_session.get(Conversation, conversation_id)
    assert conversation.knowledge_base_id is None



def test_chat_endpoint_rejects_other_users_conversation(client, make_user, make_conversation):
    owner = make_user("owner_chat", "secret123")
    intruder = make_user("intruder_chat", "secret123")
    conversation = make_conversation(owner.id, title="owner convo")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)
    response = client.post(
        "/api/chat",
        data={
            "message": "hello",
            "scenario": "test_case_generation",
            "conversation_id": conversation.id,
        },
    )

    assert response.status_code == 404



def test_chat_endpoint_rejects_blank_message(logged_in_client, make_conversation, db_session):
    user = db_session.query(User).filter(User.username == "alice").first()
    conversation = make_conversation(user.id)

    response = logged_in_client.post(
        "/api/chat",
        data={
            "message": "   ",
            "scenario": "test_case_generation",
            "conversation_id": conversation.id,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "消息不能为空"



def test_chat_endpoint_rejects_blank_scenario(logged_in_client, make_conversation, db_session):
    user = db_session.query(User).filter(User.username == "alice").first()
    conversation = make_conversation(user.id)

    response = logged_in_client.post(
        "/api/chat",
        data={
            "message": "hello",
            "scenario": "   ",
            "conversation_id": conversation.id,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "缺少场景"



def test_plain_product_manual_prompt_does_not_use_kb_fallback_template():
    prompt = get_prompt(
        "product_manual_plain",
        history="history",
        question="how to configure this",
        context="",
        knowledge_base_name="无",
    )

    assert "【知识库未收录提示】" not in prompt
    assert "当前您正在使用的" not in prompt
    assert "当前未选择知识库" in prompt
