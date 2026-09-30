import asyncio
import logging
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from config import get_workflow_checkpoint_db_path
from workflows.nodes import (
    coverage_check,
    human_review,
    load_requirement,
    persist_failure,
    requirement_analysis_agent,
    retrieve_knowledge,
    test_case_generation_agent,
)
from workflows.state import TestWorkflowState

logger = logging.getLogger(__name__)

_conn: aiosqlite.Connection | None = None
_compiled = None
_init_lock = asyncio.Lock()


def _route_after_node(state: TestWorkflowState) -> str:
    """agent/确认节点后的公共路由：有错误则落库失败并结束，否则继续"""
    if state.get("error"):
        return "persist_failure"
    return "continue"


def _build_state_graph() -> StateGraph:
    builder = StateGraph(TestWorkflowState)
    builder.add_node("load_requirement", load_requirement)
    builder.add_node("retrieve_knowledge", retrieve_knowledge)
    builder.add_node("requirement_analysis_agent", requirement_analysis_agent)
    builder.add_node("human_review", human_review)
    builder.add_node("test_case_generation_agent", test_case_generation_agent)
    builder.add_node("coverage_check", coverage_check)
    builder.add_node("persist_failure", persist_failure)

    builder.add_edge(START, "load_requirement")
    builder.add_conditional_edges(
        "load_requirement",
        _route_after_node,
        {"continue": "retrieve_knowledge", "persist_failure": "persist_failure"},
    )
    builder.add_edge("retrieve_knowledge", "requirement_analysis_agent")
    builder.add_conditional_edges(
        "requirement_analysis_agent",
        _route_after_node,
        {"continue": "human_review", "persist_failure": "persist_failure"},
    )
    builder.add_conditional_edges(
        "human_review",
        _route_after_node,
        {"continue": "test_case_generation_agent", "persist_failure": "persist_failure"},
    )
    builder.add_conditional_edges(
        "test_case_generation_agent",
        _route_after_node,
        {"continue": "coverage_check", "persist_failure": "persist_failure"},
    )
    builder.add_edge("coverage_check", END)
    builder.add_edge("persist_failure", END)
    return builder


async def get_compiled_graph():
    """编译并缓存工作流图（checkpointer 为 SQLite 文件，thread_id 取 workflow_id）"""
    global _conn, _compiled
    if _compiled is None:
        async with _init_lock:
            if _compiled is None:
                path = get_workflow_checkpoint_db_path()
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                conn = aiosqlite.connect(path)
                await conn  # 启动 aiosqlite 后台线程
                saver = AsyncSqliteSaver(conn)
                await saver.setup()
                _conn = conn
                _compiled = _build_state_graph().compile(checkpointer=saver)
                logger.info("工作流图已编译，checkpoint 数据库: %s", path)
    return _compiled
