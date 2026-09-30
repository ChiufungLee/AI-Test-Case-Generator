"""普通附件临时向量化检索测试"""
import pytest

from utils import retriever


class _StubConfig:
    model = "stub-embedding"
    dimensions = 3
    encoding_format = "float"


class _KeywordEmbeddingsAPI:
    """文本含"账户锁定"时返回与查询同向的向量，否则返回正交向量"""

    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        texts = kwargs["input"]
        if isinstance(texts, str):
            texts = [texts]
        data = [
            type("EmbeddingData", (), {"embedding": [1.0, 0.0, 0.0] if "账户锁定" in t else [0.0, 1.0, 0.0]})()
            for t in texts
        ]
        return type("Response", (), {"data": data})()


@pytest.fixture()
def keyword_embedding(monkeypatch):
    api = _KeywordEmbeddingsAPI()
    client = type("Client", (), {"embeddings": api})()
    monkeypatch.setattr(retriever, "get_embedding_config", lambda: _StubConfig())
    monkeypatch.setattr(retriever, "get_async_embedding_client", lambda: client)
    return api


def _big_doc(paragraphs: int = 8) -> str:
    """构造足够长的文档：每段约 900+ 字符，保证分块后超过 top_k"""
    parts = []
    for i in range(paragraphs):
        if i == paragraphs // 2:
            parts.append(f"第{i}段：目标段落，账户锁定30分钟的规则说明。" + "锁" * 900)
        else:
            parts.append(f"第{i}段：无关内容。" + "填" * 900)
    return "\n\n".join(parts)


@pytest.mark.asyncio
async def test_retrieve_from_plain_text_ranks_relevant_chunk_first(keyword_embedding):
    excerpt = await retriever.retrieve_from_plain_text(_big_doc(), "账户锁定30分钟的规则")

    assert "目标段落" in excerpt
    assert "账户锁定30分钟的规则说明" in excerpt
    # 只返回 top_k 个片段
    assert excerpt.count("\n\n---\n\n") == retriever.PLAIN_DOC_TOP_K - 1
    # 确实走了 embedding（块数超过 top_k）
    assert keyword_embedding.calls >= 1


@pytest.mark.asyncio
async def test_retrieve_from_plain_text_short_text_skips_embedding(monkeypatch):
    api = _KeywordEmbeddingsAPI()
    client = type("Client", (), {"embeddings": api})()
    monkeypatch.setattr(retriever, "get_embedding_config", lambda: _StubConfig())
    monkeypatch.setattr(retriever, "get_async_embedding_client", lambda: client)

    short_text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
    excerpt = await retriever.retrieve_from_plain_text(short_text, "任意查询")

    assert excerpt == short_text
    assert api.calls == 0


@pytest.mark.asyncio
async def test_retrieve_from_plain_text_empty_text_returns_empty(keyword_embedding):
    assert await retriever.retrieve_from_plain_text("   \n  ", "查询") == ""
