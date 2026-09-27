import asyncio
import functools
import json
import logging
import re
from typing import AsyncGenerator, List, Union

import httpx
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from config import (
    get_chroma_config,
    get_embedding_client,
    get_embedding_config,
    get_llm_config,
    get_retriever_config,
)

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from langchain.chat_models import init_chat_model
from models.chat import Conversation, Message
from models.database import create_session
from prompts.prompts import get_prompt, get_scenario_temperature

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _get_cached_llm_model():
    """初始化并缓存大语言模型实例"""
    config = get_llm_config()

    timeout = httpx.Timeout(
        connect=config.timeout_connect,
        read=config.timeout_read,
        write=config.timeout_write,
        pool=config.timeout_pool,
    )

    model = init_chat_model(
        model=config.model,
        model_provider=config.provider,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout=timeout,
        max_retries=config.max_retries,
    )

    return model


def reset_llm_state():
    """清除 LLM 模型实例与配置缓存（测试隔离用）"""
    _get_cached_llm_model.cache_clear()
    get_llm_config.cache_clear()
    get_embedding_config.cache_clear()
    get_embedding_client.cache_clear()
    get_retriever_config.cache_clear()
    get_chroma_config.cache_clear()




async def call_llm_model(prompt: Union[str, List[BaseMessage]], temperature: float | None = None) -> AsyncGenerator[str, None]:
    """异步调用LLM模型并流式返回token"""
    model = _get_cached_llm_model()
    full_response = ""

    llm_input: Union[str, List[BaseMessage]]
    if isinstance(prompt, str):
        llm_input = [HumanMessage(content=prompt)]
    else:
        llm_input = prompt

    stream_kwargs = {}
    if temperature is not None:
        stream_kwargs["temperature"] = temperature

    try:
        aiter = model.astream(llm_input, **stream_kwargs).__aiter__()
        try:
            while True:
                try:
                    token = await asyncio.wait_for(aiter.__anext__(), timeout=180)
                except StopAsyncIteration:
                    break
                yield token.content
                full_response += token.content
        finally:
            await aiter.aclose()

        if not full_response:
            # 模型返回空流（连接正常闭合但 0 个 token）：输出可感知的兜底提示，
            # 避免前端渲染出空白回复
            logger.warning("LLM返回空流（0 token），prompt长度: %s", len(prompt))
            yield "[错误：模型未返回内容，请稍后重试]"

    except asyncio.TimeoutError:
        yield "[错误：生成响应超时]"
        logger.warning("LLM生成超时，prompt长度: %s", len(prompt))
    except Exception as e:
        yield f"[错误：生成失败 - {str(e)}]"
        logger.error("LLM调用异常: %s", e, exc_info=True)
    finally:
        if full_response:
            logger.debug("完整响应长度: %s", len(full_response))


async def generate_response(
    request,
    prompt: Union[str, List[BaseMessage]],
    conversation_id: str,
    is_new_conversation: bool,
    message: str,
    temperature: float | None = None,
):
    """流式生成 AI 回复。

    不依赖请求作用域的 db 会话（FastAPI 0.116 中该会话在响应体发送前即关闭），
    落库操作在生成器内经 asyncio.to_thread 自建会话完成，且不阻塞事件循环。
    """
    ai_response = ""
    completed = False
    title_task = (
        asyncio.create_task(generate_and_update_title(message, conversation_id))
        if is_new_conversation
        else None
    )

    try:
        async for token in call_llm_model(prompt, temperature=temperature):
            if await request.is_disconnected():
                logger.info("客户端已断开连接")
                return

            ai_response += token
            yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
        completed = True
    except GeneratorExit:
        logger.info("流式响应被中断")
    finally:
        logger.info("AI响应结束，长度: %s", len(ai_response))

        if ai_response:
            # 断连/中断时也保存已生成的部分，避免用户提问后内容全丢
            await asyncio.to_thread(_save_message, conversation_id, "assistant", ai_response)
            await asyncio.to_thread(_touch_conversation, conversation_id)

        if completed:
            if title_task is not None:
                conversation_title = await title_task
                if conversation_title:
                    yield f"data: {json.dumps({'conversation_title': conversation_title}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"


async def generate_regenerate_response(
    request,
    prompt: Union[str, List[BaseMessage]],
    conversation_id: str,
    old_ai_message_id: int,
    temperature: float | None = None,
):
    """重新生成AI响应：输出完成后删除旧AI消息并保存新消息（会话自管，不依赖请求作用域 db）"""
    ai_response = ""
    completed = False

    try:
        async for token in call_llm_model(prompt, temperature=temperature):
            if await request.is_disconnected():
                logger.info("客户端已断开连接")
                return

            ai_response += token
            yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
        completed = True
    except GeneratorExit:
        logger.info("流式响应被中断")
    finally:
        logger.info("重新生成AI响应结束，长度: %s", len(ai_response))

        if completed and ai_response:
            await asyncio.to_thread(_delete_message, old_ai_message_id)
            await asyncio.to_thread(_save_message, conversation_id, "assistant", ai_response)
            await asyncio.to_thread(_touch_conversation, conversation_id)

        if completed:
            yield "data: [DONE]\n\n"


def _save_message(conversation_id: str, role: str, content: str) -> None:
    """落库一条消息（阻塞操作，经 asyncio.to_thread 调用；自建会话用完即关）"""
    db = create_session()
    try:
        db.add(Message(conversation_id=conversation_id, role=role, content=content))
        db.commit()
        logger.info("保存%s消息成功", "用户" if role == "user" else "AI")
    except Exception as e:
        db.rollback()
        logger.error("保存消息失败: %s", e, exc_info=True)
    finally:
        db.close()


def _touch_conversation(conversation_id: str) -> None:
    """刷新会话的 updated_at（阻塞操作，经 asyncio.to_thread 调用）"""
    db = create_session()
    try:
        db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=func.now())
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _delete_message(message_id: int) -> None:
    """删除指定消息（阻塞操作，经 asyncio.to_thread 调用）"""
    db = create_session()
    try:
        message = db.query(Message).filter(Message.id == message_id).first()
        if message:
            db.delete(message)
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error("删除消息失败: %s", e, exc_info=True)
    finally:
        db.close()


def _set_conversation_title(conversation_id: str, title: str) -> None:
    """更新对话标题（阻塞操作，经 asyncio.to_thread 调用）"""
    db = create_session()
    try:
        db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(title=title)
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("更新对话标题失败: %s", e, exc_info=True)
    finally:
        db.close()


async def generate_and_update_title(user_message: str, conversation_id: str):
    """生成并更新对话标题（自带会话管理，可在独立任务中运行）"""

    fallback_title = (user_message[:20] + "...") if len(user_message) > 20 else user_message

    try:
        title_system = get_prompt(scenario="title_generation", question=user_message)
        title_temperature = get_scenario_temperature("title_generation")
        model = _get_cached_llm_model()

        response = await model.ainvoke(
            [SystemMessage(content=title_system), HumanMessage(content=user_message)],
            temperature=title_temperature,
            max_tokens=50,
        )
        title_str = response.content

        title = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5\s]", "", title_str).strip() or fallback_title

        if len(title) > 30:
            title = title[:30] + "..."

        await asyncio.to_thread(_set_conversation_title, conversation_id, title)
        return title
    except Exception as e:
        logger.error("生成标题失败: %s", e, exc_info=True)
        await asyncio.to_thread(_set_conversation_title, conversation_id, fallback_title)
        return fallback_title
