import functools
import logging
import os
import uuid
from typing import List, Optional

import chromadb
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from unstructured.chunking.title import chunk_by_title
from unstructured.partition.pdf import partition_pdf

from config import (
    get_chroma_config,
    get_embedding_client,
    get_embedding_config, 
    get_rag_db_path,
    get_temp_upload_dir,
    get_upload_dir,
)

logger = logging.getLogger(__name__)


def ensure_storage_dirs():
    os.makedirs(get_upload_dir(), exist_ok=True)
    os.makedirs(get_temp_upload_dir(), exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_chromadb_client():
    ensure_storage_dirs()
    return chromadb.PersistentClient(path=get_rag_db_path())



class DocumentProcessor:
    @property
    def client(self):
        return get_embedding_client()

    @property
    def embedding_config(self):
        return get_embedding_config()
    
    @property
    def chromadb_client(self):
        return get_chromadb_client()

    def embed(self, text: str) -> List[float]:
        """生成单条文本的嵌入向量"""
        try:
            config = self.embedding_config

            response = self.client.embeddings.create(
                model=config.model,
                input=text,
                dimensions=config.dimensions,
                encoding_format=config.encoding_format,
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error("Embedding生成失败: %s", e, exc_info=True)
            return []

    def embed_batch(self, texts: List[str], batch_size: int = 10) -> List[List[float]]:
        """批量生成嵌入向量，text-embedding-v4 支持多输入；单批失败重试一次"""
        vectors: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_vectors = self._embed_one_batch(batch)
            if batch_vectors is None:
                logger.warning("批量Embedding失败 (batch %d-%d)，重试一次", i, i + len(batch))
                batch_vectors = self._embed_one_batch(batch)
            if batch_vectors is None:
                logger.error("批量Embedding重试后仍失败 (batch %d-%d)", i, i + len(batch))
                batch_vectors = [[] for _ in batch]
            vectors.extend(batch_vectors)
        return vectors

    def _embed_one_batch(self, batch: List[str]) -> Optional[List[List[float]]]:
        try:
            config = self.embedding_config

            response = self.client.embeddings.create(
                model=config.model,
                input=batch,
                dimensions=config.dimensions,
                encoding_format=config.encoding_format,
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            logger.error("批量Embedding失败: %s", e, exc_info=True)
            return None

    def load_pdf(self, file_path: str) -> List[Document]:
        """使用 unstructured 加载 PDF 并按文档结构分块"""
        try:
            elements = partition_pdf(filename=file_path, strategy="fast", languages=["zh"])
            logger.info("partition_pdf 提取元素数: %d", len(elements))
            if not elements:
                logger.warning("partition_pdf 未提取到任何元素，尝试回退方案")
                return self._load_pdf_fallback(file_path)

            chunks = chunk_by_title(
                elements,
                max_characters=1500,
                combine_text_under_n_chars=500,
                overlap=100,
            )
            logger.info("chunk_by_title 生成块数: %d", len(chunks))

            documents = []
            for chunk in chunks:
                text = str(chunk).strip()
                if not text:
                    continue
                metadata = {"source": file_path}
                if hasattr(chunk, "metadata") and chunk.metadata:
                    page_num = getattr(chunk.metadata, "page_number", None)
                    if page_num is not None:
                        metadata["page"] = page_num
                documents.append(Document(page_content=text, metadata=metadata))

            if not documents:
                logger.warning("结构分块后无有效文档，尝试回退方案")
                return self._load_pdf_fallback(file_path)

            # 对仍超过 2000 字符的 chunk 做二次分割
            result = []
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=1500,
                chunk_overlap=200,
                separators=["\n\n", "。", "！", "？", "\n", " ", ""],
            )
            for doc in documents:
                if len(doc.page_content) > 2000:
                    result.extend(splitter.split_documents([doc]))
                else:
                    result.append(doc)

            logger.info("PDF 加载完成: 结构分块数=%d, 最终分块数=%d", len(documents), len(result))
            return result
        except Exception as e:
            logger.error("PDF加载失败: %s", e, exc_info=True)
            raise

    def _load_pdf_fallback(self, file_path: str) -> List[Document]:
        """回退方案：使用 PyPDFLoader + RecursiveCharacterTextSplitter"""
        from langchain_community.document_loaders import PyPDFLoader

        logger.info("使用回退方案（PyPDFLoader + RecursiveCharacterTextSplitter）")
        loader = PyPDFLoader(file_path)
        docs = loader.load()
        logger.info("PyPDFLoader 加载页数: %d", len(docs))
        for i, doc in enumerate(docs):
            raw_len = len(doc.page_content)
            doc.page_content = doc.page_content.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
            logger.info("页面 %d: 原始长度=%d, 清理后长度=%d", i, raw_len, len(doc.page_content))

        docs = [doc for doc in docs if doc.page_content]
        logger.info("有效页面数: %d", len(docs))

        if not docs:
            logger.warning("PyPDFLoader 提取到的所有页面内容为空，尝试OCR方案")
            return self._load_pdf_with_ocr(file_path)

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            separators=["\n\n", "。", "！", "？", "\n", " ", ""],
        )
        result = splitter.split_documents(docs)
        logger.info("回退方案生成分片数: %d", len(result))

        if not result:
            logger.warning("回退方案分片数为0，尝试OCR方案")
            return self._load_pdf_with_ocr(file_path)

        return result

    def _load_pdf_with_ocr(self, file_path: str) -> List[Document]:
        """OCR回退方案：用于图片型PDF"""
        logger.info("使用OCR方案提取PDF内容")
        try:
            elements = partition_pdf(
                filename=file_path,
                strategy="hi_res",
                languages=["zh"],
            )
            logger.info("OCR方案提取元素数: %d", len(elements))
            if not elements:
                logger.warning("OCR方案也未提取到任何内容")
                return []

            chunks = chunk_by_title(
                elements,
                max_characters=1500,
                combine_text_under_n_chars=500,
                overlap=100,
            )

            documents = []
            for chunk in chunks:
                text = str(chunk).strip()
                if not text:
                    continue
                metadata = {"source": file_path}
                if hasattr(chunk, "metadata") and chunk.metadata:
                    page_num = getattr(chunk.metadata, "page_number", None)
                    if page_num is not None:
                        metadata["page"] = page_num
                documents.append(Document(page_content=text, metadata=metadata))

            if not documents:
                logger.warning("OCR方案未生成有效文档")
                return []

            splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000,
                chunk_overlap=200,
                separators=["\n\n", "。", "！", "？", "\n", " ", ""],
            )
            result = splitter.split_documents(documents)
            logger.info("OCR方案生成分片数: %d", len(result))
            return result
        except Exception as e:
            logger.error(
                "OCR方案失败（可能缺少OCR依赖）: %s",
                e,
            )
            return []

    def split_documents(
        self,
        docs: list[Document],
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> list[Document]:
        """文档分块（已由 load_pdf 完成结构感知分块，直接返回）"""
        return docs

    def save_to_chroma(
        self,
        splits: List[Document],
        collection_name: str,
        file_metadata: Optional[dict] = None,
    ) -> int:
        """保存文档分片到ChromaDB（批量 embedding + 单次写入）"""
        try:
            embedding_config = get_embedding_config()
            chroma_config = get_chroma_config()
            collection = self.chromadb_client.get_or_create_collection(
                name=collection_name,
                metadata={
                    "hnsw:space": chroma_config.distance_metric,
                    "embedding_model": embedding_config.model,
                    "embedding_dimensions": str(
                        embedding_config.dimensions
                    ),
                },
            )

            logger.info("save_to_chroma: 收到 %d 个分片", len(splits))

            # 收集有效分片
            ids = []
            documents = []
            metadatas = []
            for split in splits:
                if not split.page_content.strip():
                    continue
                doc_id = f"{collection_name}_{uuid.uuid4().hex}"
                metadata = split.metadata.copy() if split.metadata else {}
                if file_metadata:
                    metadata.update(file_metadata)
                ids.append(doc_id)
                documents.append(split.page_content)
                metadatas.append(metadata)

            if not documents:
                logger.warning("save_to_chroma: 无有效分片")
                return 0

            # 批量生成 embeddings
            vectors = self.embed_batch(documents)

            # 过滤 embedding 失败的分片
            valid_ids, valid_docs, valid_vectors, valid_metadatas = [], [], [], []
            for doc_id, doc, vec, meta in zip(ids, documents, vectors, metadatas):
                if vec:
                    valid_ids.append(doc_id)
                    valid_docs.append(doc)
                    valid_vectors.append(vec)
                    valid_metadatas.append(meta)

            embed_fail = len(documents) - len(valid_docs)
            if embed_fail > 0:
                logger.warning("save_to_chroma: %d 个分片 embedding 失败被跳过", embed_fail)

            if not valid_ids:
                return 0

            # 单次批量写入 ChromaDB
            collection.add(
                ids=valid_ids,
                documents=valid_docs,
                embeddings=valid_vectors,
                metadatas=valid_metadatas,
            )
            logger.info("成功保存 %d 个分片到集合 %s", len(valid_ids), collection_name)
            return len(valid_ids)

        except Exception as e:
            logger.error("保存到ChromaDB失败: %s", e, exc_info=True)
            raise

    def delete_documents_by_file_id(self, collection_name: str, file_id: str) -> bool:
        """按文件ID删除ChromaDB中的文档分片"""
        try:
            collection = self.chromadb_client.get_collection(name=collection_name)
            collection.delete(where={"file_id": file_id})
            logger.info("成功删除集合 %s 中 file_id=%s 的文档分片", collection_name, file_id)
            return True
        except Exception as e:
            logger.error("删除文档分片失败: %s", e, exc_info=True)
            return False

    def delete_collection(self, collection_name: str) -> bool:
        """删除ChromaDB集合"""
        try:
            self.chromadb_client.delete_collection(name=collection_name)
            logger.info("成功删除集合: %s", collection_name)
            return True
        except Exception as e:
            logger.error("删除集合失败: %s", e, exc_info=True)
            return False

    def get_collection_info(self, collection_name: str) -> Optional[dict]:
        """获取集合信息"""
        try:
            collection = self.chromadb_client.get_collection(name=collection_name)
            return {
                "name": collection_name,
                "count": collection.count(),
                "metadata": collection.metadata,
            }
        except Exception as e:
            logger.error("获取集合信息失败: %s", e, exc_info=True)
            return None


@functools.lru_cache(maxsize=1)
def get_document_processor() -> DocumentProcessor:
    ensure_storage_dirs()
    return DocumentProcessor()


def reset_document_processor_state():
    """清除文档处理器与 ChromaDB 客户端缓存（测试隔离用）"""
    get_document_processor.cache_clear()
    get_chromadb_client.cache_clear()
