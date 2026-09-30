"""存量数据兼容回归：skipped_chunks 列是后补的（INTEGER NULL），旧行为 NULL，
曾导致 /api/knowledge-bases/ 响应校验 500（int 字段收到 None）。"""
from sqlalchemy import text

from models.database import _ensure_schema_updates, get_engine, init_db
from models.knowledge_models import KnowledgeFile
from models.user import User


def test_api_tolerates_legacy_null_skipped_chunks(
    logged_in_client, make_knowledge_base, make_knowledge_file, db_session
):
    alice = db_session.query(User).filter(User.username == "alice").first()
    kb = make_knowledge_base(alice.id, name="legacy kb")
    make_knowledge_file(kb.id)
    db_session.query(KnowledgeFile).update({"skipped_chunks": None}, synchronize_session=False)
    db_session.commit()

    response = logged_in_client.get("/api/knowledge-bases/")

    assert response.status_code == 200
    files = [f for kb_payload in response.json() for f in kb_payload["files"]]
    assert files
    assert all(f["skipped_chunks"] == 0 for f in files)


def test_schema_update_backfills_null_skipped_chunks(
    db_session, make_user, make_knowledge_base, make_knowledge_file
):
    owner = make_user("legacy_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="legacy kb")
    make_knowledge_file(kb.id)
    db_session.query(KnowledgeFile).update({"skipped_chunks": None}, synchronize_session=False)
    db_session.commit()

    init_db()
    _ensure_schema_updates(get_engine())

    db_session.expire_all()
    assert all(f.skipped_chunks == 0 for f in db_session.query(KnowledgeFile).all())
