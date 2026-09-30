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

    extra_kwargs = {}
    if not config.enable_thinking:
        # DeepSeek 思考模式默认开启（effort=high），非标准参数须经 OpenAI SDK
        # 的 extra_body 传递；思考模式的 reasoning token 计入 max_tokens 且
        # temperature 不生效
        extra_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    model = init_chat_model(
        model=config.model,
        model_provider=config.provider,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout=timeout,
        max_retries=config.max_retries,
        **extra_kwargs,
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


def get_llm_model():
    """获取缓存的 LLM 模型实例（供工作流等其他模块复用，测试经 reset_llm_state 隔离）"""
    return _get_cached_llm_model()




# 空正文重试的输出预算：思考模型（deepseek-v4 系列）的 reasoning token 计入
# max_tokens，复杂提示词可能把配置预算全部耗尽在推理上导致正文为空，重试时
# 至少放大到此值；若配置值更大则按配置值的 2 倍放大
EMPTY_STREAM_RETRY_MAX_TOKENS = 16384


class LLMGenerationError(Exception):
    """LLM 生成失败。

    详细异常只写日志（可能含 URL、模型名等敏感信息），user_message 是
    面向用户的通用提示，经 SSE 的 {"error": ...} 事件下发、不落库。
    """

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


def _retry_max_tokens() -> int:
    try:
        configured = get_llm_config().max_tokens
    except Exception:
        return EMPTY_STREAM_RETRY_MAX_TOKENS
    return max(EMPTY_STREAM_RETRY_MAX_TOKENS, configured * 2)


async def call_llm_model(prompt: Union[str, List[BaseMessage]], temperature: float | None = None) -> AsyncGenerator[str, None]:
    """异步调用LLM模型并流式返回token。

    思考模型可能把输出预算全部耗尽在 reasoning 上（正文为空、finish_reason=length），
    首次尝试正文为空时自动放大 max_tokens 重试一次。
    生成失败时不 yield 错误文本（避免被当作回复内容落库），改为抛出 LLMGenerationError，
    由调用方决定如何向用户提示。
    """
    model = _get_cached_llm_model()

    llm_input: Union[str, List[BaseMessage]]
    if isinstance(prompt, str):
        llm_input = [HumanMessage(content=prompt)]
    else:
        llm_input = prompt

    base_kwargs = {}
    if temperature is not None:
        base_kwargs["temperature"] = temperature

    full_response = ""
    for attempt in range(2):
        stream_kwargs = dict(base_kwargs)
        if attempt > 0:
            stream_kwargs["max_tokens"] = _retry_max_tokens()

        attempt_response = ""
        try:
            aiter = model.astream(llm_input, **stream_kwargs).__aiter__()
            try:
                while True:
                    try:
                        token = await asyncio.wait_for(aiter.__anext__(), timeout=180)
                    except StopAsyncIteration:
                        break
                    yield token.content
                    attempt_response += token.content
            finally:
                await aiter.aclose()
        except asyncio.TimeoutError:
            logger.warning("LLM生成超时，prompt长度: %s", len(prompt))
            raise LLMGenerationError("生成响应超时，请稍后重试") from None
        except Exception as e:
            logger.error("LLM调用异常: %s", e, exc_info=True)
            raise LLMGenerationError("生成失败，请稍后重试") from e

        full_response += attempt_response
        if attempt_response.strip():
            logger.debug("完整响应长度: %s", len(full_response))
            return

        if attempt == 0:
            logger.warning(
                "LLM首次流式返回空正文（疑似思考模型 reasoning 耗尽 max_tokens），"
                "以 max_tokens=%s 重试一次，prompt长度: %s",
                _retry_max_tokens(), len(prompt),
            )

    logger.warning("LLM重试后仍返回空正文，prompt长度: %s", len(prompt))
    raise LLMGenerationError("模型未返回内容，请稍后重试")


def _error_sse_event(message: str) -> str:
    """把用户可读的失败提示包装为独立 SSE 事件（不进入消息内容、不落库）"""
    return f"data: {json.dumps({'error': message}, ensure_ascii=False)}\n\n"


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
    error_message = None
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
    except LLMGenerationError as e:
        error_message = e.user_message
    finally:
        logger.info("AI响应结束，长度: %s", len(ai_response))

        if ai_response:
            # 断连/中断时也保存已生成的部分，避免用户提问后内容全丢；
            # 失败提示只经 error 事件下发，不进入消息内容
            await asyncio.to_thread(_save_message, conversation_id, "assistant", ai_response)
            await asyncio.to_thread(_touch_conversation, conversation_id)

        if completed:
            if title_task is not None:
                conversation_title = await title_task
                if conversation_title:
                    yield f"data: {json.dumps({'conversation_title': conversation_title}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        elif error_message:
            yield _error_sse_event(error_message)


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
    error_message = None

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
    except LLMGenerationError as e:
        error_message = e.user_message
    finally:
        logger.info("重新生成AI响应结束，长度: %s", len(ai_response))

        if completed and ai_response:
            await asyncio.to_thread(_delete_message, old_ai_message_id)
            await asyncio.to_thread(_save_message, conversation_id, "assistant", ai_response)
            await asyncio.to_thread(_touch_conversation, conversation_id)

        if completed:
            yield "data: [DONE]\n\n"
        elif error_message:
            # 失败时旧回复保持不变，仅下发错误提示
            yield _error_sse_event(error_message)


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


# 标题生成的 token 上限：思考模型（deepseek-v4 系列）的 reasoning token 计入
# max_tokens，预算过小会把正文清空、导致标题永远走截断兜底，必须留足思考空间
TITLE_MAX_TOKENS = 512


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
            max_tokens=TITLE_MAX_TOKENS,
        )
        title_str = response.content
        if not (title_str or "").strip():
            logger.warning("标题生成返回空正文（思考模型 reasoning 可能耗尽 max_tokens），使用截断兜底")

        title = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5\s]", "", title_str).strip() or fallback_title

        if len(title) > 30:
            title = title[:30] + "..."

        await asyncio.to_thread(_set_conversation_title, conversation_id, title)
        return title
    except Exception as e:
        logger.error("生成标题失败: %s", e, exc_info=True)
        await asyncio.to_thread(_set_conversation_title, conversation_id, fallback_title)
        return fallback_title
