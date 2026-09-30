import uuid
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class Conversation(Base):
    """对话表"""
    __tablename__ = "conversations"
    
    # 注意：MySQL 的 VARCHAR 必须指定长度，UUID 长度为 36
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(200), default="新对话")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    scenario = Column(String(50), default="requirement_clarification")  # 对话场景
    knowledge_base_id = Column(String(36), ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    
    # 关系
    # 消息按自增 id 排序：timestamp 为秒级精度，同秒消息顺序不稳定
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id")
    user = relationship("User", back_populates="conversations")
    
    def __repr__(self):
        return f"<Conversation(id={self.id}, title='{self.title}')>"

class Message(Base):
    """消息表"""
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # "user", "assistant", "system"
    content = Column(Text, nullable=False)  # 使用 Text 类型存储长文本
    timestamp = Column(DateTime, default=func.now())
    # 附件名与附件正文单独存储，不拼进 content：正文保持纯提问文本，
    # 重新生成时才能用干净的提问做检索，并从 attachment_text 恢复普通对话的附件上下文
    attachment_name = Column(String(255), nullable=True)
    # 普通对话附件的文档正文（知识库路径的正文已向量化入库，无需保存）；
    # MySQL 用 MEDIUMTEXT 容纳 3 万字符的中文文本（TEXT 只有 64KB 字节）
    attachment_text = Column(
        MEDIUMTEXT().with_variant(Text(), "sqlite"),
        nullable=True,
    )
    
    # 关系
    conversation = relationship("Conversation", back_populates="messages")
    
    def __repr__(self):
        return f"<Message(id={self.id}, role='{self.role}', content='{self.content[:50]}...')>"