"""向量与文件处理一致性的回归测试：

- 向量删除失败时中止删除文件记录（残留分片不再继续参与检索）
- 部分分片向量化失败按阈值标记 failed / completed，并记录 skipped_chunks
- 服务启动时重置卡在 pending/processing 的文件；失败文件可重试
- 同一知识库内相同内容的文件按内容哈希去重
"""

import pytest

from models.knowledge_models import KnowledgeFile
from services import knowledge_service
from services.knowledge_service import (
    EMBED_PARTIAL_FAILURE_RATIO,
    process_document_async,
    reset_stale_processing_files,
)


def _make_file_record(db_session, kb, test_env, filename="dup.pdf", content=b"%PDF-1.4\n%stub\n", status="completed"):
    upload_dir = test_env["upload_dir"]
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / filename
    file_path.write_bytes(content)
    record = KnowledgeFile(
        knowledge_base_id=kb.id,
        filename=filename,
        file_path=str(file_path.resolve()),
        file_size=len(content),
        file_type="pdf",
        status=status,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def test_delete_file_aborts_when_vector_delete_fails(
    db_session, make_user, make_knowledge_base, test_env, document_processor, monkeypatch
):
    owner = make_user("vecdel_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="vecdel kb", collection_name="vecdel_collection")
    record = _make_file_record(db_session, kb, test_env)

    def failing_delete(collection_name, file_id):
        return False

    monkeypatch.setattr(document_processor, "delete_documents_by_file_id", failing_delete)

    with pytest.raises(Exception) as exc_info:
        knowledge_service.delete_knowledge_file(db_session, kb, record)

    assert "向量删除失败" in str(exc_info.value.detail)
    # 记录保留，不产生孤儿向量对应的悬空记录
    db_session.expire_all()
    assert db_session.query(KnowledgeFile).filter(KnowledgeFile.id == record.id).first() is not None


def test_delete_file_proceeds_when_collection_missing(
    db_session, make_user, make_knowledge_base, test_env, document_processor
):
    """集合不存在（从未成功写入向量）时删除应正常进行"""
    owner = make_user("vecdel_owner2", "secret123")
    kb = make_knowledge_base(owner.id, name="vecdel kb 2", collection_name="never_created_collection")
    record = _make_file_record(db_session, kb, test_env)

    knowledge_service.delete_knowledge_file(db_session, kb, record)

    db_session.expire_all()
    assert db_session.query(KnowledgeFile).filter(KnowledgeFile.id == record.id).first() is None


def test_partial_embed_failure_above_threshold_marks_failed(
    db_session, make_user, make_knowledge_base, test_env, document_processor, monkeypatch
):
    owner = make_user("partial_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="partial kb", collection_name="partial_collection")
    record = _make_file_record(db_session, kb, test_env, filename="partial.pdf")

    # 4 个分片中 1 个分片批次 embedding 失败（比例 0.25 >= 阈值）
    monkeypatch.setattr(
        document_processor, "load_pdf",
        lambda path: [type("D", (), {"page_content": f"块{i}", "metadata": {}})() for i in range(4)],
    )
    import utils.file_handle as fh

    real_embed_batch = fh.DocumentProcessor.embed_batch

    def flaky_embed_batch(self, texts, batch_size=10):
        vectors = real_embed_batch(self, texts, batch_size)
        vectors[0] = []  # 第一批失败
        return vectors

    monkeypatch.setattr(fh.DocumentProcessor, "embed_batch", flaky_embed_batch)

    process_document_async(record.id, kb.id)

    db_session.expire_all()
    refreshed = db_session.query(KnowledgeFile).filter(KnowledgeFile.id == record.id).first()
    assert refreshed.status == "failed"
    assert refreshed.skipped_chunks == 1
    assert "向量化失败" in refreshed.error
    # 已成功的分片已写入向量库（供重试前清理）
    collection = document_processor.chromadb_client.get_collection("partial_collection")
    assert collection.count() == 3


def test_partial_embed_failure_below_threshold_stays_completed(
    db_session, make_user, make_knowledge_base, test_env, document_processor, monkeypatch
):
    owner = make_user("partial_owner2", "secret123")
    kb = make_knowledge_base(owner.id, name="partial kb 2", collection_name="partial_collection_2")
    record = _make_file_record(db_session, kb, test_env, filename="partial2.pdf")

    # 20 个分片失败 1 个（比例 0.05 < 阈值）：completed 但记录 skipped_chunks
    monkeypatch.setattr(
        document_processor, "load_pdf",
        lambda path: [type("D", (), {"page_content": f"块{i}", "metadata": {}})() for i in range(20)],
    )
    import utils.file_handle as fh

    real_embed_batch = fh.DocumentProcessor.embed_batch

    def flaky_embed_batch(self, texts, batch_size=10):
        vectors = real_embed_batch(self, texts, batch_size)
        vectors[0] = []
        return vectors

    monkeypatch.setattr(fh.DocumentProcessor, "embed_batch", flaky_embed_batch)

    assert 1 / 20 < EMBED_PARTIAL_FAILURE_RATIO
    process_document_async(record.id, kb.id)

    db_session.expire_all()
    refreshed = db_session.query(KnowledgeFile).filter(KnowledgeFile.id == record.id).first()
    assert refreshed.status == "completed"
    assert refreshed.skipped_chunks == 1
    assert refreshed.chunk_count == 19
    assert "跳过" in refreshed.error


def test_reset_stale_processing_files(db_session, make_user, make_knowledge_base, test_env):
    owner = make_user("stale_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="stale kb", collection_name="stale_collection")
    pending = _make_file_record(db_session, kb, test_env, filename="a.pdf", status="pending")
    processing = _make_file_record(db_session, kb, test_env, filename="b.pdf", status="processing")
    completed = _make_file_record(db_session, kb, test_env, filename="c.pdf", status="completed")

    reset_count = reset_stale_processing_files()

    assert reset_count == 2
    db_session.expire_all()
    assert db_session.query(KnowledgeFile).filter(KnowledgeFile.id == pending.id).first().status == "failed"
    assert db_session.query(KnowledgeFile).filter(KnowledgeFile.id == processing.id).first().status == "failed"
    assert db_session.query(KnowledgeFile).filter(KnowledgeFile.id == completed.id).first().status == "completed"


def test_retry_failed_file_requeues_and_cleans_vectors(
    db_session, make_user, make_knowledge_base, test_env, document_processor
):
    owner = make_user("retry_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="retry kb", collection_name="retry_collection")
    record = _make_file_record(db_session, kb, test_env, filename="retry.pdf", status="failed")

    # 模拟上次失败时已写入部分向量
    collection = document_processor.chromadb_client.get_or_create_collection("retry_collection")
    collection.add(
        ids=["old_1", "old_2"],
        documents=["d1", "d2"],
        embeddings=[[0.1], [0.2]],
        metadatas=[{"file_id": record.id}, {"file_id": record.id}],
    )
    record.error = "所有分片向量化失败"
    db_session.commit()

    retried = knowledge_service.retry_knowledge_file(db_session, kb.id, record.id)

    assert retried.status == "pending"
    assert retried.error is None
    assert collection.count() == 0


def test_retry_rejects_non_failed_status(db_session, make_user, make_knowledge_base, test_env):
    owner = make_user("retry_owner2", "secret123")
    kb = make_knowledge_base(owner.id, name="retry kb 2", collection_name="retry_collection_2")
    processing = _make_file_record(db_session, kb, test_env, filename="p.pdf", status="processing")
    completed = _make_file_record(db_session, kb, test_env, filename="c.pdf", status="completed")

    import fastapi

    with pytest.raises(fastapi.HTTPException) as exc_info:
        knowledge_service.retry_knowledge_file(db_session, kb.id, processing.id)
    assert exc_info.value.status_code == 400

    with pytest.raises(fastapi.HTTPException) as exc_info:
        knowledge_service.retry_knowledge_file(db_session, kb.id, completed.id)
    assert exc_info.value.status_code == 400


def test_retry_endpoint_requires_ownership(client, make_user, make_knowledge_base, test_env):
    owner = make_user("retry_page_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="retry kb 3", collection_name="retry_collection_3")

    from models.database import create_session

    file_path = test_env["upload_dir"] / "x.pdf"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"%PDF-1.4\n%stub\n")
    temp_db = create_session()
    try:
        record = KnowledgeFile(
            knowledge_base_id=kb.id,
            filename="x.pdf",
            file_path=str(file_path.resolve()),
            file_size=16,
            file_type="pdf",
            status="failed",
        )
        temp_db.add(record)
        temp_db.commit()
        temp_db.refresh(record)
        record_id = record.id
    finally:
        temp_db.close()

    client.post("/login", data={"username": "retry_page_owner", "password": "secret123"}, follow_redirects=False)
    response = client.post(f"/api/knowledge-bases/{kb.id}/files/{record_id}/retry")
    assert response.status_code == 200
    assert response.json()["file_id"] == record_id

    # 其他用户不可重试
    other = make_user("retry_intruder", "secret123")
    client.post("/login", data={"username": other.username, "password": "secret123"}, follow_redirects=False)
    response = client.post(f"/api/knowledge-bases/{kb.id}/files/{record_id}/retry")
    assert response.status_code == 404


def test_duplicate_upload_rejected_by_content_hash(logged_in_client, document_processor, db_session):
    first = logged_in_client.post(
        "/api/knowledge-bases/",
        json={"name": "hash kb", "description": "desc"},
    )
    kb_id = first.json()["id"]

    payload = {"file": ("doc.pdf", b"%PDF-1.4\nsame-content\n", "application/pdf")}
    ok = logged_in_client.post(f"/api/knowledge-bases/{kb_id}/upload", files=payload)
    assert ok.status_code == 200

    # 同内容（不同文件名）再次上传被拒绝
    dup = logged_in_client.post(
        f"/api/knowledge-bases/{kb_id}/upload",
        files={"file": ("doc-rename.pdf", b"%PDF-1.4\nsame-content\n", "application/pdf")},
    )
    assert dup.status_code == 409
    assert "相同内容" in dup.json()["detail"]

    # 不同内容可上传
    different = logged_in_client.post(
        f"/api/knowledge-bases/{kb_id}/upload",
        files={"file": ("other.pdf", b"%PDF-1.4\nother-content\n", "application/pdf")},
    )
    assert different.status_code == 200


def test_failed_duplicate_can_be_reuploaded(db_session, make_user, make_knowledge_base, test_env, document_processor):
    """去重只拦截未失败的记录：failed 记录同哈希可重新上传，completed 记录同哈希被拒"""
    import fastapi

    owner = make_user("hash_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="hash kb 2", collection_name="hash_collection_2")

    failed = _make_file_record(db_session, kb, test_env, filename="old.pdf", status="failed")
    failed.content_hash = "0" * 64
    db_session.commit()

    completed = _make_file_record(db_session, kb, test_env, filename="done.pdf", status="completed")
    completed.content_hash = "1" * 64
    db_session.commit()

    # 与 failed 记录同哈希：放行（允许重新上传替代手动重试）
    knowledge_service._ensure_no_duplicate_file(db_session, kb.id, "0" * 64)

    # 与 completed 记录同哈希：拒绝
    with pytest.raises(fastapi.HTTPException) as exc_info:
        knowledge_service._ensure_no_duplicate_file(db_session, kb.id, "1" * 64)
    assert exc_info.value.status_code == 409
    assert exc_info.value.status_code == 409

    # 与 completed 同哈希但 ID 对不上？——不，直接构造一条 completed 记录验证拦截
    completed.content_hash = "1" * 64
    db_session.commit()
    with pytest.raises(fastapi.HTTPException):
        knowledge_service._ensure_no_duplicate_file(db_session, kb.id, "1" * 64)
