import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db
from prompts.prompts import get_prompt, get_prompt_messages, get_scenario_temperature
from services import knowledge_service
from services.auth_service import AuthService
from services.chat_service import ChatService
from utils.data_handle import convert_table_to_csv, extract_table_from_markdown
from utils.llm_handle import generate_regenerate_response, generate_response
from utils.retriever import get_rag_retriever_by_kb

app = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
logger = logging.getLogger(__name__)

# 对话历史条数上限；testcase_generation 场景的提示词更长，使用更短的历史
HISTORY_LIMITS = {"testcase_generation": 7}
DEFAULT_HISTORY_LIMIT = 10

def _build_chat_messages(scenario, message, history_messages, knowledge_base_name, context, use_knowledge_base: bool) -> tuple[list[BaseMessage], str]:
    prompt_scenario = scenario if use_knowledge_base else f"{scenario}_plain"
    messages = get_prompt_messages(
        prompt_scenario,
        history_messages=history_messages,
        context=context,
        question=message,
        knowledge_base_name=knowledge_base_name,
    )
    return messages, prompt_scenario


async def _load_chat_context(message: str, knowledge_base_id: str | None, db: Session, user_id: int):
    context = ""
    knowledge_base_name = "无"

    if not knowledge_base_id:
        return context, knowledge_base_name

    knowledge_base = await knowledge_service.get_knowledge_base_by_id(kb_id=knowledge_base_id, db=db, user_id=user_id, allow_shared_read=True)
    if not knowledge_base:
        return context, knowledge_base_name

    knowledge_base_name = knowledge_base.name
    retriever = await get_rag_retriever_by_kb(knowledge_base, db, user_id)
    if retriever:
        try:
            docs = await retriever.get_relevant_documents(message)
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


class ChatRequest(BaseModel):
    message: str
    scenario: str
    conversation_id: str
    knowledge_base_id: str | None = None


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    username = request.session.get("username")
    if username is None:
        return templates.TemplateResponse(request, "login.html", {"error": "用户会话已失效，请重新登录"})
    return templates.TemplateResponse(request, "index.html", {"username": username, "user_id": request.session.get("user_id")})


@app.get("/api/history")
async def get_history(
    request: Request,
    scenario: str,
    knowledge_base_id: str | None = None,
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()
    conversation_groups = await ChatService.get_conversation_groups(user_id, scenario, knowledge_base_id, db)
    return {"groups": conversation_groups}


@app.get("/api/conversation/{conversation_id}")
async def get_conversation(
    request: Request,
    conversation_id: str,
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()

    conversation_messages = await ChatService.get_conversation_message(user_id, conversation_id, db)
    if not conversation_messages:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    return {"messages": conversation_messages}


@app.post("/api/conversation/new")
async def create_new_conversation(
    request: Request,
    scenario: str = Form(...),
    knowledge_base_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()

    title = "新对话"
    new_conversation = await ChatService.create_new_conversation(
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


@app.post("/api/chat")
async def chat_endpoint(
    request: Request,
    data: ChatRequest,
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()

    message = data.message.strip()
    scenario = data.scenario.strip()
    conversation_id = data.conversation_id.strip()
    knowledge_base_id = data.knowledge_base_id or None

    if not message:
        return JSONResponse(status_code=400, content={"error": "消息不能为空"})
    if not scenario:
        return JSONResponse(status_code=400, content={"error": "缺少场景"})
    if not conversation_id:
        return JSONResponse(status_code=400, content={"error": "缺少会话ID"})

    conversation = await ChatService.get_conversation_info(conversation_id, db, user_id=user_id)
    if not conversation:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    is_new_conversation = conversation.title == "新对话"
    history_limit = HISTORY_LIMITS.get(scenario, DEFAULT_HISTORY_LIMIT)
    history_messages = await ChatService.get_conversation_history_messages(conversation_id, db, limit=history_limit)

    context, knowledge_base_name = await _load_chat_context(message, knowledge_base_id, db, user_id)
    use_knowledge_base = bool(knowledge_base_id)
    messages, prompt_scenario = _build_chat_messages(
        scenario,
        message,
        history_messages,
        knowledge_base_name,
        context,
        use_knowledge_base=use_knowledge_base,
    )
    temperature = get_scenario_temperature(prompt_scenario)

    # 用户消息在流式响应开始前落库：即使生成失败或客户端断连，提问也不会丢失
    await ChatService.create_new_message(conversation.id, "user", message, db)
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


@app.delete("/api/conversation/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()

    delete_result = await ChatService.delete_conversation(user_id, conversation_id, db)
    if not delete_result:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    return JSONResponse(content={"message": "对话删除成功"})


@app.post("/api/conversation/{conversation_id}/rename")
async def rename_conversation(
    conversation_id: str,
    request: Request,
    data: dict,
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()

    new_title = data.get("title", "").strip()
    if not new_title:
        return JSONResponse(status_code=400, content={"error": "标题不能为空"})

    rename_result = await ChatService.rename_conversation(user_id, conversation_id, new_title, db)
    if not rename_result:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    return rename_result


@app.get("/api/export/testcases")
async def export_testcases(
    request: Request,
    conversation_id: str,
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()

    ai_messages = await ChatService.get_conversation_ai_message(user_id, conversation_id, db)
    if not ai_messages:
        return JSONResponse(status_code=404, content={"error": "未找到测试用例"})

    latest_ai_message = ai_messages[0].content
    table_data = extract_table_from_markdown(latest_ai_message)
    if not table_data:
        return JSONResponse(status_code=404, content={"error": "未找到表格数据"})

    csv_data = convert_table_to_csv(table_data)
    headers = {
        "Content-Disposition": f"attachment; filename=testcases_{conversation_id}.csv",
        "Content-Type": "text/csv",
    }
    return Response(content=csv_data, headers=headers)


class RegenerateRequest(BaseModel):
    conversation_id: str
    message: str | None = None


@app.post("/api/chat/regenerate")
async def regenerate_endpoint(
    request: Request,
    data: RegenerateRequest,
    db: Session = Depends(get_db),
):
    user_id = AuthService.get_optional_request_user_id(request)
    if user_id is None:
        return AuthService.unauthorized_json_response()

    conversation_id = data.conversation_id.strip()
    if not conversation_id:
        return JSONResponse(status_code=400, content={"error": "缺少会话ID"})

    conversation = await ChatService.get_conversation_info(conversation_id, db, user_id=user_id)
    if not conversation:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})

    last_user_msg = await ChatService.get_last_user_message(conversation_id, db)
    if not last_user_msg:
        return JSONResponse(status_code=400, content={"error": "没有可重新生成的消息"})

    old_ai_msg = await ChatService.get_last_ai_message(conversation_id, db)
    if not old_ai_msg:
        return JSONResponse(status_code=400, content={"error": "没有可重新生成的AI回复"})

    # 如果传入了编辑后的消息，更新用户消息内容
    edited_message = (data.message or "").strip()
    if edited_message:
        last_user_msg.content = edited_message
        db.commit()

    message = last_user_msg.content
    scenario = conversation.scenario
    knowledge_base_id = conversation.knowledge_base_id

    history_limit = HISTORY_LIMITS.get(scenario, DEFAULT_HISTORY_LIMIT) + 1
    history_messages = await ChatService.get_conversation_history_messages(conversation_id, db, limit=history_limit)
    # 排除最后一轮用户消息（当前要重新回答的问题）
    if history_messages and isinstance(history_messages[-1], HumanMessage):
        history_messages = history_messages[:-1]

    context, knowledge_base_name = await _load_chat_context(message, knowledge_base_id, db, user_id)
    use_knowledge_base = bool(knowledge_base_id)
    messages, prompt_scenario = _build_chat_messages(
        scenario,
        message,
        history_messages,
        knowledge_base_name,
        context,
        use_knowledge_base=use_knowledge_base,
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
