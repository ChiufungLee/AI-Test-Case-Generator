from pathlib import Path

from models.knowledge_models import KnowledgeFile
from services.knowledge_service import process_document_async



def test_background_processing_handles_deleted_file_race(db_session, make_user, make_knowledge_base, test_env):
    owner = make_user("race_owner", "secret123")
    kb = make_knowledge_base(owner.id, name="race kb", collection_name="race_collection")

    upload_dir = Path(test_env["upload_dir"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / "race.pdf"
    file_path.write_bytes(b"%PDF-1.4\n%stub\n")

    record = KnowledgeFile(
        knowledge_base_id=kb.id,
        filename="race.pdf",
        file_path=str(file_path.resolve()),
        file_size=file_path.stat().st_size,
        file_type="pdf",
        status="pending",
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    record_id = record.id

    file_path.unlink()

    process_document_async(record_id, kb.id)

    db_session.expire_all()
    refreshed = db_session.query(KnowledgeFile).filter(KnowledgeFile.id == record_id).first()
    assert refreshed is None
