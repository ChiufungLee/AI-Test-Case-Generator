import pytest

from utils.retriever import get_rag_retriever_by_kb


@pytest.mark.asyncio
async def test_retriever_cache_isolated_by_user(db_session, make_user, make_knowledge_base, document_processor):
    owner = make_user("retriever_owner", "secret123")
    intruder = make_user("retriever_intruder", "secret123")
    kb = make_knowledge_base(owner.id, name="secure kb", collection_name="secure_collection")

    document_processor.chromadb_client.get_or_create_collection(kb.collection_name)

    owner_retriever = await get_rag_retriever_by_kb(kb.id, db_session, user_id=owner.id)
    intruder_retriever = await get_rag_retriever_by_kb(kb.id, db_session, user_id=intruder.id)

    assert owner_retriever is not None
    assert intruder_retriever is None
