import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db
from prompts.prompts import SCENARIO_PROMPTS, get_prompt, get_prompt_messages, get_scenario_temperature
from services import knowledge_service
from services.auth_service import require_user
from services.chat_service import ChatService
from utils.data_handle import convert_table_to_csv, extract_table_from_markdown
from utils.llm_handle import generate_regenerate_response, generate_response, rewrite_retrieval_query
from utils.retriever import get_rag_retriever_by_kb, retrieve_from_plain_text

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
logger = logging.getLogger(__name__)

# 对话历史条数上限；testcase_generation 场景的提示词更长，使用更短的历史
HISTORY_LIMITS = {"testcase_generation": 7}
DEFAULT_HISTORY_LIMIT = 10

# 历史消息的 token 预算（估算值）：条数之外的第二道限制，防止大表格历史撑爆上下文
HISTORY_TOKEN_BUDGET = 4000

# 普通附件：全文直读上限；超过则临时向量化检索节选。提取硬上限防止极端大文档
MAX_PLAIN_DOC_CHARS = 30_000
MAX_PLAIN_DOC_EXTRACT_CHARS = 150_000


def _unknown_scenario_response(scenario: str):
    return JSONResponse(status_code=400, content={"error": f"未知场景: {scenario}"})


def _cap_text(text: str, cap: int) -> str:
    if len(text) > cap:
        return text[:cap] + f"\n\n（文档过长，仅展示前 {cap} 字符）"
    return text

def _build_chat_messages(scenario, message, history_messages, knowledge_base_name, context, use_knowledge_base: bool, context_intro: str | None = None) -> tuple[list[BaseMessage], str]:
    prompt_scenario = scenario if use_knowledge_base else f"{scenario}_plain"
    messages = get_prompt_messages(
        prompt_scenario,
        history_messages=history_messages,
        context=context,
        context_intro=context_intro,
        question=message,
        knowledge_base_name=knowledge_base_name,
    )
    return messages, prompt_scenario


async def _load_chat_context(
    message: str,
    knowledge_base_id: str | None,
    db: Session,
    user_id: int,
    history_messages: list[BaseMessage] | None = None,
):
    context = ""
    knowledge_base_name = "无"

    if not knowledge_base_id:
        return context, knowledge_base_name

    knowledge_base = await asyncio.to_thread(
        knowledge_service.get_knowledge_base_by_id,
        kb_id=knowledge_base_id,
        db=db,
        user_id=user_id,
        allow_shared_read=True,
    )
    if not knowledge_base:
        return context, knowledge_base_name

    knowledge_base_name = knowledge_base.name
    retriever = await get_rag_retriever_by_kb(knowledge_base, db, user_id)
    if retriever:
        try:
            # 多轮追问先用 query rewrite 改写成独立查询，再检索，
            # 避免"那超时怎么处理？"这类指代性提问检索不到内容
            retrieval_query = await rewrite_retrieval_query(history_messages or [], message)
            docs = await retriever.get_relevant_documents(retrieval_query)
            parts = []
            for doc in docs:
                filename = doc.metadata.get("filename", "未知文件")
                page = doc.metadata.get("page")
                source = f"《{filename}》"
                if page:
                    source += f" 第{page}页"
                parts.append(f"[来源: {source}]\n{doc.page_content}")
            context = "\n\n---\n\n".join(parts)
            logger.info("从知识库 %s 检索到 %s 个相关文档", knowledge_base_id, len(docs))
        except Exception as e:
            logger.error("检索失败: %s", e, exc_info=True)

    return context, knowledge_base_name


async def _build_plain_doc_context(
    attachment_name: str | None,
    doc_text: str,
    message: str,
    history_messages: list[BaseMessage],
) -> tuple[str, str | None]:
    """普通附件 → (context, context_intro)。

    短文档全文注入；长文档临时向量化后按相关度节选（失败回退为截断展示）。
    """
    if len(doc_text) <= MAX_PLAIN_DOC_CHARS:
        return doc_text, f"以下是用户上传的文档《{attachment_name}》的内容："

    try:
        retrieval_query = await rewrite_retrieval_query(history_messages, message)
        excerpt = await retrieve_from_plain_text(doc_text, retrieval_query)
    except Exception as e:
        logger.warning("普通附件临时检索失败，回退为截断展示: %s", e)
        excerpt = ""

    if excerpt:
        return (
            excerpt,
            f"以下是用户上传的文档《{attachment_name}》中与问题最相关的片段（文档过长，已按相关度节选）：",
        )
    return _cap_text(doc_text, MAX_PLAIN_DOC_CHARS), f"以下是用户上传的文档《{attachment_name}》的内容："


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    username = request.session.get("username")
    if username is None:
        return templates.TemplateResponse(request, "login.html", {"error": "用户会话已失效，请重新登录"})
    return templates.TemplateResponse(request, "index.html", {"username": username, "user_id": request.session.get("user_id")})


@router.get("/api/history")
def get_history(
    scenario: str,
    user_id: int = Depends(require_user),
    knowledge_base_id: str | None = None,
    db: Session = Depends(get_db),
):
    return {"groups": ChatService.get_conversation_groups(user_id, scenario, knowledge_base_id, db)}


@router.get("/api/conversation/{conversation_id}")
def get_conversation(
    conversation_id: str,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):

    conversation_messages = ChatService.get_conversation_message(user_id, conversation_id, db)
    if not conversation_messages:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    return {
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "attachment_name": message.attachment_name,
            }
            for message in conversation_messages
        ]
    }


@router.post("/api/conversation/new")
def create_new_conversation(
    scenario: str = Form(...),
    knowledge_base_id: str | None = Form(None),
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    if scenario not in SCENARIO_PROMPTS:
        return _unknown_scenario_response(scenario)

    title = "新对话"
    new_conversation = ChatService.create_new_conversation(
        user_id=user_id,
        title=title,
        scenario=scenario,
        knowledge_base_id=knowledge_base_id,
        db=db,
    )

    return {
        "conversation_id": new_conversation.id,
        "title": new_conversation.title,
    }


def _truncate_plain_doc_text(text: str) -> str:
    """提取阶段硬上限：极端大文档只保留前段并附说明，超出部分不参与检索"""
    return _cap_text(text, MAX_PLAIN_DOC_EXTRACT_CHARS)


async def _attachment_processing_stream(filename: str):
    """附件已登记、后台向量化中的提示流：不调用 LLM、不落库消息。

    文档就绪前检索不到其内容，完成后再提问即可被检索覆盖。
    """
    yield "data: " + json.dumps({"attachment_processing": filename}, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


@router.post("/api/chat")
async def chat_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(require_user),
    message: str = Form(...),
    scenario: str = Form(...),
    conversation_id: str = Form(...),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):

    message = (message or "").strip()
    scenario = (scenario or "").strip()
    conversation_id = (conversation_id or "").strip()

    if not message:
        return JSONResponse(status_code=400, content={"error": "消息不能为空"})
    if not scenario:
        return JSONResponse(status_code=400, content={"error": "缺少场景"})
    if not conversation_id:
        return JSONResponse(status_code=400, content={"error": "缺少会话ID"})

    conversation = await asyncio.to_thread(ChatService.get_conversation_info, conversation_id, db, user_id=user_id)
    if not conversation:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    if scenario not in SCENARIO_PROMPTS:
        return _unknown_scenario_response(scenario)

    # 知识库统一以会话记录为准，避免与请求参数不一致导致检索/重新生成行为漂移
    knowledge_base_id = conversation.knowledge_base_id
    is_new_conversation = conversation.title == "新对话"

    if file is not None and file.filename and knowledge_base_id:
        # 知识库附件：登记后交后台任务向量化，不阻塞聊天请求（大 PDF 解析+向量化很慢）。
        # 不落库消息、不调用 LLM——就绪前检索不到该文档，避免给出缺上下文的回答；
        # 前端提示用户稍后重新提问
        try:
            record = await knowledge_service.register_chat_attachment(db, file, knowledge_base_id, user_id)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.detail})
        background_tasks.add_task(knowledge_service.process_document_async, record.id, knowledge_base_id)
        return StreamingResponse(
            _attachment_processing_stream(record.filename),
            media_type="text/event-stream",
        )

    if file is not None and file.filename:
        try:
            text = await knowledge_service.extract_pdf_text(file)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.detail})
        if not text.strip():
            return JSONResponse(status_code=400, content={"error": "无法从文档中提取到文本内容"})
        attachment_name, plain_doc_context = file.filename, _truncate_plain_doc_text(text)
    else:
        attachment_name, plain_doc_context = None, None

    history_limit = HISTORY_LIMITS.get(scenario, DEFAULT_HISTORY_LIMIT)
    history_messages = await asyncio.to_thread(
        ChatService.get_conversation_history_messages,
        conversation_id,
        db,
        limit=history_limit,
        max_tokens=HISTORY_TOKEN_BUDGET,
    )

    context_intro = None
    if knowledge_base_id:
        context, knowledge_base_name = await _load_chat_context(
            message, knowledge_base_id, db, user_id, history_messages=history_messages
        )
    elif plain_doc_context:
        context, context_intro = await _build_plain_doc_context(
            attachment_name, plain_doc_context, message, history_messages
        )
        knowledge_base_name = "无"
    else:
        context, knowledge_base_name = "", "无"

    use_knowledge_base = bool(knowledge_base_id)
    messages, prompt_scenario = _build_chat_messages(
        scenario,
        message,
        history_messages,
        knowledge_base_name,
        context,
        use_knowledge_base=use_knowledge_base,
        context_intro=context_intro,
    )
    temperature = get_scenario_temperature(prompt_scenario)

    # 用户消息在流式响应开始前落库：即使生成失败或客户端断连，提问也不会丢失；
    # 附件名与附件正文存独立字段，content 保持纯提问文本，重新生成时可直接复用
    await asyncio.to_thread(
        ChatService.create_new_message,
        conversation.id,
        "user",
        message,
        db,
        attachment_name=attachment_name,
        attachment_text=plain_doc_context,
    )
    return StreamingResponse(
        generate_response(
            request,
            messages,
            conversation.id,
            is_new_conversation,
            message,
            temperature=temperature,
        ),
        media_type="text/event-stream",
    )


@router.delete("/api/conversation/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):

    delete_result = ChatService.delete_conversation(user_id, conversation_id, db)
    if not delete_result:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    return JSONResponse(content={"message": "对话删除成功"})


@router.post("/api/conversation/{conversation_id}/rename")
def rename_conversation(
    conversation_id: str,
    data: dict,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):

    new_title = data.get("title", "").strip()
    if not new_title:
        return JSONResponse(status_code=400, content={"error": "标题不能为空"})

    rename_result = ChatService.rename_conversation(user_id, conversation_id, new_title, db)
    if not rename_result:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    return rename_result


@router.get("/api/export/testcases")
def export_testcases(
    conversation_id: str,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):

    ai_messages = ChatService.get_conversation_ai_message(user_id, conversation_id, db)
    if not ai_messages:
        return JSONResponse(status_code=404, content={"error": "未找到测试用例"})

    latest_ai_message = ai_messages[0].content
    table_data = extract_table_from_markdown(latest_ai_message)
    if not table_data:
        return JSONResponse(status_code=404, content={"error": "未找到表格数据"})

    csv_data = convert_table_to_csv(table_data)
    headers = {
        "Content-Disposition": f"attachment; filename=testcases_{conversation_id}.csv",
        "Content-Type": "text/csv; charset=utf-8",
    }
    # utf-8-sig 带 BOM，保证中文在 Excel 中不乱码
    return Response(content=csv_data.encode("utf-8-sig"), headers=headers)


class RegenerateRequest(BaseModel):
    conversation_id: str
    message: str | None = None


@router.post("/api/chat/regenerate")
async def regenerate_endpoint(
    request: Request,
    data: RegenerateRequest,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):

    conversation_id = data.conversation_id.strip()
    if not conversation_id:
        return JSONResponse(status_code=400, content={"error": "缺少会话ID"})

    conversation = await asyncio.to_thread(ChatService.get_conversation_info, conversation_id, db, user_id=user_id)
    if not conversation:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    last_user_msg = await asyncio.to_thread(ChatService.get_last_user_message, conversation_id, db)
    if not last_user_msg:
        return JSONResponse(status_code=400, content={"error": "没有可重新生成的消息"})

    old_ai_msg = await asyncio.to_thread(ChatService.get_last_ai_message, conversation_id, db)
    if not old_ai_msg:
        return JSONResponse(status_code=400, content={"error": "没有可重新生成的AI回复"})

    # 如果传入了编辑后的消息，更新用户消息内容
    edited_message = (data.message or "").strip()
    if edited_message:
        last_user_msg.content = edited_message
        db.commit()

    message = last_user_msg.content
    # 兼容历史数据：旧版本把附件标记拼进了 content，检索前剥掉
    if message.startswith("【附件: ") and "\n" in message:
        first_line, rest = message.split("\n", 1)
        if first_line.endswith("】"):
            message = rest
            last_user_msg.content = message
            db.commit()

    scenario = conversation.scenario
    if scenario not in SCENARIO_PROMPTS:
        return _unknown_scenario_response(scenario)
    knowledge_base_id = conversation.knowledge_base_id

    history_limit = HISTORY_LIMITS.get(scenario, DEFAULT_HISTORY_LIMIT) + 1
    history_messages = await asyncio.to_thread(
        ChatService.get_conversation_history_messages,
        conversation_id,
        db,
        limit=history_limit,
        max_tokens=HISTORY_TOKEN_BUDGET,
    )
    # 排除最后一轮用户消息（当前要重新回答的问题）
    if history_messages and isinstance(history_messages[-1], HumanMessage):
        history_messages = history_messages[:-1]

    # 附件上下文与首次提问时保持一致：知识库路径重新检索，普通对话路径
    # 复用落库的附件正文（attachment_text），不丢失上下文
    context_intro = None
    if knowledge_base_id:
        context, knowledge_base_name = await _load_chat_context(
            message, knowledge_base_id, db, user_id, history_messages=history_messages
        )
    elif last_user_msg.attachment_text:
        context, context_intro = await _build_plain_doc_context(
            last_user_msg.attachment_name, last_user_msg.attachment_text, message, history_messages
        )
        knowledge_base_name = "无"
    else:
        context, knowledge_base_name = "", "无"

    use_knowledge_base = bool(knowledge_base_id)
    messages, prompt_scenario = _build_chat_messages(
        scenario,
        message,
        history_messages,
        knowledge_base_name,
        context,
        use_knowledge_base=use_knowledge_base,
        context_intro=context_intro,
    )
    temperature = get_scenario_temperature(prompt_scenario)

    return StreamingResponse(
        generate_regenerate_response(
            request,
            messages,
            conversation.id,
            old_ai_msg.id,
            temperature=temperature,
        ),
        media_type="text/event-stream",
    )
