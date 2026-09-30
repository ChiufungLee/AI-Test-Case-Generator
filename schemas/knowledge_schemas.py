
### knowledge pydantic 验证
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Optional


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    visibility: str = "private"  # "private" / "shared"

class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[str] = None  # "private" / "shared"

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