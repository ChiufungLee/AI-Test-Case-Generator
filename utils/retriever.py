import asyncio
import logging
import math
from threading import Lock
from typing import List

import chromadb
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy.orm import Session

from config import (
    get_async_embedding_client,
    get_embedding_config,
    get_retriever_config,
)
from utils import file_handle


logger = logging.getLogger(__name__)

# 普通附件临时检索的分块与节选参数（与 PyPDFLoader 回退路径的分块口径一致）
PLAIN_DOC_CHUNK_SIZE = 1000
PLAIN_DOC_CHUNK_OVERLAP = 200
PLAIN_DOC_TOP_K = 6


class ChromaRetriever:
    def __init__(
        self,
        collection_name: str,
        chroma_client: chromadb.Client,
    ):
        self.collection_name = collection_name
        self.chroma_client = chroma_client
        self.embedding_config = get_embedding_config()
        self.embedding_client = get_async_embedding_client()
        self.retriever_config = get_retriever_config()
        self.collection = self.chroma_client.get_collection(name=collection_name)

    async def embed(self, text: str) -> List[float]:
        config = self.embedding_config

        response = await self.embedding_client.embeddings.create(
            model=config.model,
            input=text,
            dimensions=config.dimensions,
            encoding_format=config.encoding_format,
        )
        logger.debug("embedding token 使用量: %s", response.usage.total_tokens)
        return response.data[0].embedding

    async def get_relevant_documents(
        self,
        query: str,
        n_results: int | None = None,

    ) -> List[Document]:
        """从ChromaDB中检索与查询相关的文档。

        先请求两倍数量的候选结果，再根据距离阈值过滤掉不相关的文档，
        最终返回最多 n_results 条高相关度结果。

        Args:
            query: 用户查询文本。
            n_results: 最终返回的最大文档数量，默认3。
        """

        config = self.retriever_config

        if n_results is None:
            n_results = config.top_k

        candidate_k = max(
            config.candidate_k,
            n_results,
        )

        candidate_k = min(
            candidate_k,
            (await asyncio.to_thread(self.collection.count)) or candidate_k,
        )

        query_vector = await self.embed(query)
        results = await asyncio.to_thread(
            self.collection.query,
            query_embeddings=[query_vector],
            n_results=candidate_k,
            include=["documents", "metadatas", "distances"],
        )

        documents = []
        documents_groups = results.get("documents") or []
        metadata_groups = results.get("metadatas") or []
        distance_groups = results.get("distances") or []
        for group_index, doc_list in enumerate(documents_groups):
            metadata_list = metadata_groups[group_index] if group_index < len(metadata_groups) else []
            distance_list = distance_groups[group_index] if group_index < len(distance_groups) else []
            for item_index, text in enumerate(doc_list):
                distance = distance_list[item_index] if item_index < len(distance_list) else float("inf")
                if (
                    config.enable_distance_filter
                    and config.distance_threshold is not None
                    and distance > config.distance_threshold
                ):
                    logger.debug(
                        "过滤低相关文档: distance=%.4f > threshold=%.4f",
                        distance,
                        config.distance_threshold,
                    )
                    continue

                metadata = metadata_list[item_index] if item_index < len(metadata_list) else {}
                documents.append(Document(page_content=text, metadata=metadata or {}))
                if len(documents) >= n_results:
                    break
            if len(documents) >= n_results:
                break

        if config.enable_distance_filter:
            logger.info(
                "检索结果: 请求%d个, 返回%d个, distance_threshold=%s",
                n_results,
                len(documents),
                config.distance_threshold,
            )
        else:
            logger.info(
                "检索结果: 请求%d个, 返回%d个, distance_filter=disabled",
                n_results,
                len(documents),
            )

        return documents

    @staticmethod
    def clear_retriever_cache(kb_id: str):
        with _retriever_lock:
            if kb_id in _retriever_cache:
                del _retriever_cache[kb_id]
                logger.info("已清除知识库 %s 的检索器缓存", kb_id)
                return True
            return False

    @staticmethod
    def clear_all_retriever_caches():
        with _retriever_lock:
            _retriever_cache.clear()
            logger.info("已清除所有检索器缓存")


_retriever_lock = Lock()
_retriever_cache = {}


async def _embed_texts(texts: List[str]) -> List[List[float]]:
    """批量向量化（单次 API 调用），返回与输入顺序一致的向量列表"""
    config = get_embedding_config()
    client = get_async_embedding_client()
    response = await client.embeddings.create(
        model=config.model,
        input=texts,
        dimensions=config.dimensions,
        encoding_format=config.encoding_format,
    )
    vectors = [item.embedding for item in response.data]
    if len(vectors) != len(texts):
        raise ValueError(f"embedding 返回数量不匹配: 期望 {len(texts)}, 实际 {len(vectors)}")
    return vectors


def _rank_by_cosine(chunks: List[str], chunk_vectors: List[List[float]], query_vector: List[float]) -> List[str]:
    def cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if not norm_a or not norm_b:
            return 0.0
        return dot / (norm_a * norm_b)

    scored = sorted(zip(chunks, chunk_vectors), key=lambda pair: cosine(query_vector, pair[1]), reverse=True)
    return [chunk for chunk, _ in scored]


async def retrieve_from_plain_text(text: str, query: str, top_k: int = PLAIN_DOC_TOP_K) -> str:
    """普通附件文本的临时向量化检索（不落库、不建集合，仅当次请求生效）。

    用于超过全文直读上限的上传文档：分块 → 批量 embedding → 余弦相似度取
    top-k 片段。块数不足 top_k 时直接返回全文分块，不调用 embedding。
    失败由调用方回退为截断展示。
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=PLAIN_DOC_CHUNK_SIZE, chunk_overlap=PLAIN_DOC_CHUNK_OVERLAP
    )
    chunks = [chunk.strip() for chunk in splitter.split_text(text) if chunk.strip()]
    if not chunks:
        return ""
    if len(chunks) <= top_k:
        return "\n\n---\n\n".join(chunks)

    chunk_vectors = await _embed_texts(chunks)
    query_vector = (await _embed_texts([query]))[0]
    ranked = _rank_by_cosine(chunks, chunk_vectors, query_vector)
    logger.info("普通附件临时检索: %s 块中节选 %s 块", len(chunks), top_k)
    return "\n\n---\n\n".join(ranked[:top_k])



async def get_rag_retriever_by_kb(kb_or_id, db: Session, user_id: int):
    # Step 1: resolve KB object; 字符串路径必须先做属主校验（在查缓存之前，
    # 防止越权用户命中其他用户缓存过的检索器）
    if isinstance(kb_or_id, str):
        from services import knowledge_service
        kb = await asyncio.to_thread(
            knowledge_service.get_knowledge_base_by_id,
            kb_id=kb_or_id,
            db=db,
            user_id=user_id,
            allow_shared_read=True,
        )
        if not kb:
            return None
        kb_id = kb.id
    else:
        kb = kb_or_id
        kb_id = kb.id

    # Step 2: fast path — check cache without lock
    if kb_id in _retriever_cache:
        logger.info("从缓存获取知识库 %s 的检索器", kb_id)
        return _retriever_cache[kb_id]

    try:
        # Step 4: create retriever
        chroma_client = file_handle.get_chromadb_client()

        try:
            collection = chroma_client.get_collection(name=kb.collection_name)
            logger.info("知识库 %s 的向量集合存在，包含 %s 个向量", kb_id, collection.count())
        except Exception as e:
            logger.error("知识库 %s 的向量集合不存在: %s", kb_id, e)
            return None

        retriever = ChromaRetriever(
            collection_name=kb.collection_name,
            chroma_client=chroma_client,
        )

        # Step 5: store in cache with double-check under lock
        with _retriever_lock:
            if kb_id in _retriever_cache:
                logger.info("检索器已被并发请求缓存，使用已有实例")
                return _retriever_cache[kb_id]
            _retriever_cache[kb_id] = retriever

        logger.info("已为知识库 %s 创建检索器，集合名称: %s", kb_id, kb.collection_name)
        return retriever
    except Exception as e:
        logger.error("创建知识库 %s 的检索器失败: %s", kb_id, e, exc_info=True)
        return None
