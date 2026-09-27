def test_logged_in_user_can_open_owned_knowledge_detail(client, make_user, make_knowledge_base):
    owner = make_user("detail_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="detail kb")

    client.post("/login", data={"username": owner.username, "password": "secret123"}, follow_redirects=False)
    response = client.get(f"/knowledge-detail?kb_id={kb.id}")

    assert response.status_code == 200



def test_knowledge_detail_requires_query_kb_id(logged_in_client):
    response = logged_in_client.get("/knowledge-detail")

    assert response.status_code == 404



def test_user_cannot_open_other_users_knowledge_detail(client, make_user, make_knowledge_base):
    owner = make_user("detail_owner_2", "secret123")
    intruder = make_user("detail_intruder", "secret123")
    kb = make_knowledge_base(owner.id, name="private detail kb")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)
    response = client.get(f"/knowledge-detail?kb_id={kb.id}")

    assert response.status_code == 404



def test_knowledge_detail_redirects_anonymous_user(client, make_user, make_knowledge_base):
    owner = make_user("detail_owner_3", "secret123")
    kb = make_knowledge_base(owner.id, name="redirect detail kb")

    response = client.get(f"/knowledge-detail?kb_id={kb.id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
