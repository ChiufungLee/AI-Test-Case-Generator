def test_user_only_sees_own_knowledge_bases(client, make_user, make_knowledge_base):
    owner = make_user("kb_owner", "secret123")
    other = make_user("kb_other", "secret123")
    own_kb = make_knowledge_base(owner.id, name="owner kb")
    make_knowledge_base(other.id, name="other kb")

    client.post("/login", data={"username": owner.username, "password": "secret123"}, follow_redirects=False)
    response = client.get("/api/knowledge-bases/")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == own_kb.id



def test_user_cannot_read_update_delete_other_users_kb(client, make_user, make_knowledge_base):
    owner = make_user("kb_owner_ops", "secret123")
    intruder = make_user("kb_intruder_ops", "secret123")
    kb = make_knowledge_base(owner.id, name="secret kb")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)

    assert client.get(f"/api/knowledge-bases/{kb.id}").status_code == 404
    assert client.put(f"/api/knowledge-bases/{kb.id}", json={"name": "hack", "description": "hack"}).status_code == 404
    assert client.delete(f"/api/knowledge-bases/{kb.id}").status_code == 404



from utils.retriever import get_rag_retriever_by_kb


def test_owner_can_upload_preview_and_delete_file(logged_in_client, document_processor):
    kb_response = logged_in_client.post(
        "/api/knowledge-bases/",
        json={"name": "Docs", "description": "desc"},
    )
    kb_id = kb_response.json()["id"]

    upload_response = logged_in_client.post(
        f"/api/knowledge-bases/{kb_id}/upload",
        files={"file": ("demo.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )
    assert upload_response.status_code == 200
    file_id = upload_response.json()["file_id"]

    kb_response = logged_in_client.get(f"/api/knowledge-bases/{kb_id}")
    kb_payload = kb_response.json()
    assert kb_payload["file_count"] == 1
    collection_name = kb_payload["collection_name"]
    collection = document_processor.chromadb_client.get_collection(collection_name)
    assert collection.count() > 0

    list_response = logged_in_client.get(f"/api/knowledge-bases/{kb_id}/files")
    assert list_response.status_code == 200
    assert any(item["id"] == file_id for item in list_response.json())

    preview_response = logged_in_client.get(f"/api/files/{file_id}/preview")
    assert preview_response.status_code == 200
    assert preview_response.headers["content-type"].startswith("application/pdf")

    delete_response = logged_in_client.delete(f"/api/knowledge-bases/{kb_id}/files/{file_id}")
    assert delete_response.status_code == 200
    assert {"file_id": file_id} in collection.deleted_wheres

    kb_after_delete = logged_in_client.get(f"/api/knowledge-bases/{kb_id}")
    assert kb_after_delete.json()["file_count"] == 0

    preview_after_delete = logged_in_client.get(f"/api/files/{file_id}/preview")
    assert preview_after_delete.status_code == 404



def test_upload_rejects_non_pdf_file(logged_in_client):
    kb_response = logged_in_client.post(
        "/api/knowledge-bases/",
        json={"name": "Docs2", "description": "desc"},
    )
    kb_id = kb_response.json()["id"]

    upload_response = logged_in_client.post(
        f"/api/knowledge-bases/{kb_id}/upload",
        files={"file": ("demo.txt", b"plain text", "text/plain")},
    )

    assert upload_response.status_code == 400
    assert upload_response.json()["detail"] == "仅支持上传 PDF 文件"



def test_anonymous_knowledge_endpoints_return_unified_401(client, make_user, make_knowledge_base, make_knowledge_file):
    owner = make_user("kb_owner_auth", "secret123")
    kb = make_knowledge_base(owner.id, name="auth kb")
    file_record = make_knowledge_file(kb.id, filename="auth.pdf")

    list_response = client.get("/api/knowledge-bases/")
    preview_response = client.get(f"/api/files/{file_record.id}/preview")
    upload_response = client.post(
        f"/api/knowledge-bases/{kb.id}/upload",
        files={"file": ("demo.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )

    assert list_response.status_code == 401
    assert list_response.json() == {"detail": "未登录"}
    assert preview_response.status_code == 401
    assert preview_response.json() == {"detail": "未登录"}
    assert upload_response.status_code == 401
    assert upload_response.json() == {"detail": "未登录"}



def test_upload_to_missing_kb_returns_404(logged_in_client):
    upload_response = logged_in_client.post(
        "/api/knowledge-bases/missing-kb/upload",
        files={"file": ("demo.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )

    assert upload_response.status_code == 404
    assert upload_response.json()["detail"] == "知识库不存在"



def test_upload_rejects_empty_file(logged_in_client):
    kb_response = logged_in_client.post(
        "/api/knowledge-bases/",
        json={"name": "DocsEmpty", "description": "desc"},
    )
    kb_id = kb_response.json()["id"]

    upload_response = logged_in_client.post(
        f"/api/knowledge-bases/{kb_id}/upload",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert upload_response.status_code == 400
    assert upload_response.json()["detail"] == "文件内容不能为空"



def test_upload_unexpected_failure_returns_500(logged_in_client, monkeypatch):
    kb_response = logged_in_client.post(
        "/api/knowledge-bases/",
        json={"name": "Docs500", "description": "desc"},
    )
    kb_id = kb_response.json()["id"]

    def broken_mkdir(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("services.knowledge_service.Path.mkdir", broken_mkdir)

    upload_response = logged_in_client.post(
        f"/api/knowledge-bases/{kb_id}/upload",
        files={"file": ("demo.pdf", b"%PDF-1.4\n%stub\n", "application/pdf")},
    )

    assert upload_response.status_code == 500
    assert upload_response.json()["detail"] == "文件上传失败"



from pathlib import Path


def test_delete_knowledge_base_cleans_uploaded_files(client, make_user, make_knowledge_base, make_knowledge_file):
    owner = make_user("kb_cleanup_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="cleanup kb", collection_name="cleanup_collection")
    file_record = make_knowledge_file(kb.id, filename="cleanup.pdf")
    from services.knowledge_service import resolve_upload_path
    file_path = resolve_upload_path(file_record.file_path)
    assert file_path.exists()

    client.post("/login", data={"username": owner.username, "password": "secret123"}, follow_redirects=False)
    response = client.delete(f"/api/knowledge-bases/{kb.id}")

    assert response.status_code == 200
    assert not file_path.exists()



def test_intruder_cannot_preview_or_delete_other_users_file(client, make_user, make_knowledge_base, make_knowledge_file):
    owner = make_user("file_owner", "secret123")
    intruder = make_user("file_intruder", "secret123")
    kb = make_knowledge_base(owner.id, name="owner files")
    file_record = make_knowledge_file(kb.id, filename="private.pdf")

    client.post("/login", data={"username": intruder.username, "password": "secret123"}, follow_redirects=False)

    preview_response = client.get(f"/api/files/{file_record.id}/preview")
    delete_response = client.delete(f"/api/knowledge-bases/{kb.id}/files/{file_record.id}")

    assert preview_response.status_code == 404
    assert delete_response.status_code == 404


def test_create_kb_collection_gets_metric_metadata(logged_in_client, document_processor):
    """创建知识库时集合带完整 metadata（度量方式 + embedding 元信息）"""
    from config import get_embedding_config

    kb_response = logged_in_client.post(
        "/api/knowledge-bases/",
        json={"name": "Meta KB", "description": "desc"},
    )
    assert kb_response.status_code == 200
    collection_name = kb_response.json()["collection_name"]

    collection = document_processor.chromadb_client.get_collection(collection_name)
    embedding_config = get_embedding_config()
    assert collection.metadata["hnsw:space"] == "cosine"
    assert collection.metadata["embedding_model"] == embedding_config.model
    assert collection.metadata["embedding_dimensions"] == str(embedding_config.dimensions)
