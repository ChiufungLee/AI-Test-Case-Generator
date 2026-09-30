"""测试专用的模块级缓存重置钩子。

这些钩子只服务于测试隔离，从生产模块挪到这里，避免生产代码携带测试设施；
实现上允许触达生产模块的内部缓存（测试代码依赖内部实现是可接受的取舍）。
"""
import asyncio
import threading

from config import (
    get_chroma_config,
    get_embedding_client,
    get_embedding_config,
    get_llm_config,
    get_retriever_config,
)
from models import database as database_module
from utils import file_handle, llm_handle, retriever
from workflows import graph


def reset_database(database_url: str | None = None):
    """关闭现有连接池并按给定 URL 重建引擎（测试隔离用）"""
    database_module.close_all_sessions()
    if database_module.engine is not None:
        database_module.engine.dispose()
    return database_module.configure_database(database_url)


def reset_document_processor_state():
    """清除文档处理器与 ChromaDB 客户端缓存（测试隔离用）"""
    file_handle.get_document_processor.cache_clear()
    file_handle.get_chromadb_client.cache_clear()


def reset_retriever_state():
    """清空知识库检索器缓存（测试隔离用）"""
    with retriever._retriever_lock:
        retriever._retriever_cache.clear()


def reset_llm_state():
    """清除 LLM 模型实例与各配置的 lru_cache（测试隔离用）"""
    llm_handle._get_cached_llm_model.cache_clear()
    get_llm_config.cache_clear()
    get_embedding_config.cache_clear()
    get_embedding_client.cache_clear()
    get_retriever_config.cache_clear()
    get_chroma_config.cache_clear()


def reset_workflow_state():
    """清除编译图缓存并关闭 checkpoint 连接（测试隔离用）。

    关闭动作在独立线程的独立事件循环中执行，无论调用方是否处于事件循环内都安全。
    """
    graph._compiled = None
    conn, graph._conn = graph._conn, None
    if conn is None:
        return

    def _close_in_new_loop():
        async def _close():
            await conn.close()

        asyncio.run(_close())

    closer = threading.Thread(target=_close_in_new_loop, daemon=True)
    closer.start()
    closer.join(timeout=5)
