
### knowledge pydantic 验证
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import List, Literal, Optional


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    visibility: Literal["private", "shared"] = "private"  # "private" / "shared"

class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[Literal["private", "shared"]] = None  # "private" / "shared"

class KnowledgeFileResponse(BaseModel):
    id: str
    filename: str
    file_size: int
    file_type: str
    status: str
    chunk_count: int
    uploaded_at: datetime
    skipped_chunks: int = 0
    error: Optional[str] = None

    @field_validator("skipped_chunks", mode="before")
    @classmethod
    def _coerce_null_skipped_chunks(cls, value):
        """存量行该列可能为 NULL（列是后补的且允许 NULL），响应层归零避免校验失败"""
        return 0 if value is None else value

class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    description: str
    collection_name: str
    file_count: int
    created_at: datetime
    updated_at: datetime
    visibility: str = "private"
    owner_user_id: Optional[int] = None
    files: List[KnowledgeFileResponse] = Field(default_factory=list)