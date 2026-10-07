from datetime import datetime, timedelta
from typing import List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from models.chat import Conversation, Message
from models.knowledge_models import KnowledgeBase


class ChatService:
    @staticmethod
    def get_conversation_groups(user_id: int, scenario: str, knowledge_base_id: str | None, db: Session):
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
    def get_user_conversation(user_id: int, conversation_id: str, db: Session) -> Optional[Conversation]:
        return ChatService._get_user_conversation_query(db, user_id).filter(Conversation.id == conversation_id).first()

    @staticmethod
    def create_new_conversation(
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
    def create_new_message(
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

    @staticmethod
    def get_conversation_history_messages(
        conversation_id: str,
        db: Session,
        limit: int = 15,
        max_tokens: int | None = None,
    ) -> List[BaseMessage]:
        """按条数上限取最近历史，再按 token 预算从最新往回裁剪。

        条数是二级限制：testcase_generation 等场景的历史里常有大表格，仅按条数
        截断很容易超预算；裁剪保持"最近连续一段"，永不丢弃最新一条消息。
        """
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

        if max_tokens is not None and len(result) > 1:
            kept: List[BaseMessage] = []
            used = 0
            for msg in reversed(result):
                cost = ChatService.estimate_text_tokens(msg.content)
                if kept and used + cost > max_tokens:
                    break
                kept.append(msg)
                used += cost
            result = list(reversed(kept))
        return result

    @staticmethod
    def estimate_text_tokens(text: str) -> int:
        """粗略 token 估算：CJK 约 1 字符/token，ASCII 约 4 字符/token。

        只用于历史预算裁剪，不追求精确；宁可高估也不低估表格体量。
        """
        if not text:
            return 0
        cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
        return cjk + (len(text) - cjk) // 4 + 1

    @staticmethod
    def get_conversation_message(user_id: int, conversation_id: str, db: Session):
        conversation = ChatService.get_user_conversation(user_id, conversation_id, db)
        if not conversation:
            return None

        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.id.asc())
            .all()
        )

    @staticmethod
    def prepare_regenerated_question(
        conversation_id: str,
        db: Session,
        edited_message: str | None = None,
    ) -> Optional[dict]:
        """重新生成前的提问整理：应用人工编辑内容 + 剥离历史遗留的附件标记。

        查询与提交都在本同步函数内完成（端点经 asyncio.to_thread 调用，
        避免在 async 端点里直接做同步 DB 操作阻塞事件循环）；
        返回 {"content", "attachment_name", "attachment_text"}，无用户消息返回 None。
        """
        last_user_msg = ChatService.get_last_user_message(conversation_id, db)
        if not last_user_msg:
            return None

        content = (edited_message or "").strip() or last_user_msg.content
        # 兼容历史数据：旧版本把附件标记拼进了 content，检索前剥掉
        if content.startswith("【附件: ") and "\n" in content:
            first_line, rest = content.split("\n", 1)
            if first_line.endswith("】"):
                content = rest

        if content != last_user_msg.content:
            last_user_msg.content = content
            db.commit()

        return {
            "content": content,
            "attachment_name": last_user_msg.attachment_name,
            "attachment_text": last_user_msg.attachment_text,
        }

    @staticmethod
    def rename_conversation(user_id: int, conversation_id: str, new_title: str, db: Session):
        conversation = ChatService.get_user_conversation(user_id, conversation_id, db)
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
    def delete_conversation(user_id: int, conversation_id: str, db: Session):
        conversation = ChatService.get_user_conversation(user_id, conversation_id, db)
        if not conversation:
            return None

        db.delete(conversation)
        db.commit()

        return {
            "success": True,
            "message": "对话删除成功",
        }

    @staticmethod
    def get_conversation_ai_message(user_id: int, conversation_id: str, db: Session):
        conversation = ChatService.get_user_conversation(user_id, conversation_id, db)
        if not conversation:
            return None

        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
            .order_by(Message.id.desc())
            .all()
        )

    @staticmethod
    def get_conversation_info(conversation_id: str, db: Session, user_id: int | None = None):
        if user_id is not None:
            return ChatService.get_user_conversation(user_id, conversation_id, db)
        return db.query(Conversation).filter(Conversation.id == conversation_id).first()

    @staticmethod
    def get_last_user_message(conversation_id: str, db: Session) -> Optional[Message]:
        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )

    @staticmethod
    def get_last_ai_message(conversation_id: str, db: Session) -> Optional[Message]:
        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
            .order_by(Message.id.desc())
            .first()
        )

    @staticmethod
    def delete_message(message_id: int, db: Session) -> bool:
        message = db.query(Message).filter(Message.id == message_id).first()
        if not message:
            return False
        db.delete(message)
        db.commit()
        return True
