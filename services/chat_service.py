from datetime import datetime, timedelta
from typing import List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from models.chat import Conversation, Message
from models.knowledge_models import KnowledgeBase


class ChatService:
    @staticmethod
    async def get_conversation_groups(user_id: int, scenario: str, knowledge_base_id: str | None, db: Session):
        today = datetime.now().date()
        three_days_ago = today - timedelta(days=3)
        one_week_ago = today - timedelta(days=7)
        filter_condition = [Conversation.user_id == user_id, Conversation.scenario == scenario]
        if knowledge_base_id:
            filter_condition.append(Conversation.knowledge_base_id == knowledge_base_id)
        else:
            filter_condition.append(Conversation.knowledge_base_id.is_(None))
        rows = (
            db.query(Conversation.id, Conversation.title, Conversation.updated_at)
            .filter(*filter_condition)
            .order_by(desc(Conversation.updated_at))
            .all()
        )

        groups = []
        today_group = {"time_group": "今日", "conversations": []}
        fewdays_group = {"time_group": "3日内", "conversations": []}
        week_group = {"time_group": "最近7天", "conversations": []}
        older_group = {"time_group": "更早", "conversations": []}

        for conv_id, title, updated_at in rows:
            conv_date = updated_at.date()
            conv_data = {
                "id": conv_id,
                "title": title,
                "updated_at": updated_at.isoformat(),
            }

            if conv_date == today:
                today_group["conversations"].append(conv_data)
            elif conv_date >= three_days_ago:
                fewdays_group["conversations"].append(conv_data)
            elif conv_date >= one_week_ago:
                week_group["conversations"].append(conv_data)
            else:
                older_group["conversations"].append(conv_data)

        if today_group["conversations"]:
            groups.append(today_group)
        if fewdays_group["conversations"]:
            groups.append(fewdays_group)
        if week_group["conversations"]:
            groups.append(week_group)
        if older_group["conversations"]:
            groups.append(older_group)

        return groups

    @staticmethod
    def _get_user_conversation_query(db: Session, user_id: int):
        return db.query(Conversation).filter(Conversation.user_id == user_id)

    @staticmethod
    async def get_user_conversation(user_id: int, conversation_id: str, db: Session) -> Optional[Conversation]:
        return ChatService._get_user_conversation_query(db, user_id).filter(Conversation.id == conversation_id).first()

    @staticmethod
    async def create_new_conversation(
        user_id: int,
        title: str,
        scenario: str,
        knowledge_base_id: str | None,
        db: Session,
    ) -> Conversation:
        knowledge_base_id = knowledge_base_id or None
        if knowledge_base_id:
            kb = (
                db.query(KnowledgeBase)
                .filter(
                    KnowledgeBase.id == knowledge_base_id,
                    or_(KnowledgeBase.owner_user_id == user_id, KnowledgeBase.visibility == "shared"),
                )
                .first()
            )
            if not kb:
                knowledge_base_id = None

        new_conversation = Conversation(
            user_id=user_id,
            title=title,
            scenario=scenario,
            knowledge_base_id=knowledge_base_id,
        )
        db.add(new_conversation)
        db.commit()
        db.refresh(new_conversation)
        return new_conversation

    @staticmethod
    async def create_new_message(
        conversation_id: str,
        role: str,
        content: str,
        db: Session,
        attachment_name: str | None = None,
        attachment_text: str | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            attachment_name=attachment_name,
            attachment_text=attachment_text,
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        return message

    # 当前未使用，保留供后续需要将历史格式化为字符串的场景（如 history_summary）
    @staticmethod
    async def get_conversation_history(conversation_id: str, db: Session, limit: int = 7) -> str:
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
            .all()
        )

        if not messages:
            return ""

        role_map = {"user": "用户", "assistant": "助手", "system": "系统"}
        lines = []
        for msg in reversed(messages):
            role = role_map.get(msg.role, msg.role)
            lines.append(f"{role}: {msg.content}")

        return "\n".join(lines)

    @staticmethod
    async def get_conversation_history_messages(conversation_id: str, db: Session, limit: int = 15) -> List[BaseMessage]:
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
            .all()
        )

        if not messages:
            return []

        role_to_class = {"user": HumanMessage, "assistant": AIMessage}
        result = []
        for msg in reversed(messages):
            cls = role_to_class.get(msg.role)
            if cls:
                result.append(cls(content=msg.content))
        return result

    @staticmethod
    async def get_conversation_message(user_id: int, conversation_id: str, db: Session):
        conversation = await ChatService.get_user_conversation(user_id, conversation_id, db)
        if not conversation:
            return None

        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.id.asc())
            .all()
        )

    @staticmethod
    async def rename_conversation(user_id: int, conversation_id: str, new_title: str, db: Session):
        conversation = await ChatService.get_user_conversation(user_id, conversation_id, db)
        if not conversation:
            return None

        conversation.title = new_title
        db.commit()
        db.refresh(conversation)

        return {
            "success": True,
            "message": "对话重命名成功",
            "conversation": conversation,
        }

    @staticmethod
    async def delete_conversation(user_id: int, conversation_id: str, db: Session):
        conversation = await ChatService.get_user_conversation(user_id, conversation_id, db)
        if not conversation:
            return None

        db.delete(conversation)
        db.commit()

        return {
            "success": True,
            "message": "对话删除成功",
        }

    @staticmethod
    async def get_conversation_ai_message(user_id: int, conversation_id: str, db: Session):
        conversation = await ChatService.get_user_conversation(user_id, conversation_id, db)
        if not conversation:
            return None

        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
            .order_by(Message.id.desc())
            .all()
        )

    @staticmethod
    async def get_conversation_info(conversation_id: str, db: Session, user_id: int | None = None):
        if user_id is not None:
            return await ChatService.get_user_conversation(user_id, conversation_id, db)
        return db.query(Conversation).filter(Conversation.id == conversation_id).first()

    @staticmethod
    async def get_last_user_message(conversation_id: str, db: Session) -> Optional[Message]:
        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )

    @staticmethod
    async def get_last_ai_message(conversation_id: str, db: Session) -> Optional[Message]:
        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
            .order_by(Message.id.desc())
            .first()
        )

    @staticmethod
    async def delete_message(message_id: int, db: Session) -> bool:
        message = db.query(Message).filter(Message.id == message_id).first()
        if not message:
            return False
        db.delete(message)
        db.commit()
        return True
