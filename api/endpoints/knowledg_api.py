import logging
import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from models.database import get_db
from models.knowledge_models import KnowledgeFile
from schemas.knowledge_schemas import KnowledgeBaseCreate, KnowledgeBaseResponse, KnowledgeBaseUpdate
from services import knowledge_service
from services.auth_service import AuthService, require_user
from utils.file_handle import get_document_processor

app = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
logger = logging.getLogger(__name__)


@app.post("/api/knowledge-bases/", response_model=KnowledgeBaseResponse)
def create_knowledge_base(
    kb_data: KnowledgeBaseCreate,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    create_knowledge = knowledge_service.create_knowledge_record(db, kb_data, owner_user_id=user_id)
    if not create_knowledge["success"]:
        raise HTTPException(status_code=400, detail=create_knowledge["message"])

    # 创建集合时统一写入度量方式与 embedding 元信息（此前裸调用
    # get_or_create_collection 不带 metadata，CHROMA_DISTANCE_METRIC 不生效）
    get_document_processor().ensure_collection(create_knowledge["knowledge_base"].collection_name)

    return create_knowledge["knowledge_base"]


@app.get("/api/knowledge-bases/", response_model=List[KnowledgeBaseResponse])
def list_knowledge_bases(user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    kbs = knowledge_service.get_all_knowledge(db, user_id=user_id)
    return kbs


@app.get("/knowledge-detail", response_class=HTMLResponse)
def knowledge_detail(request: Request, kb_id: str | None = None, db: Session = Depends(get_db)):
    if request.session.get("username") is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    user_id = AuthService.require_request_user_id(request)
    if not kb_id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    kb = knowledge_service.get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id, allow_shared_read=True)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    can_edit = kb.owner_user_id == user_id
    return templates.TemplateResponse(
        request, "knowledge_detail.html", {"kb_id": kb_id, "can_edit": can_edit, "username": request.session.get("username")}
    )


@app.get("/api/knowledge-bases/{kb_id}", response_model=KnowledgeBaseResponse)
def get_knowledge_base(kb_id: str, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    kb = knowledge_service.get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id, allow_shared_read=True)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


@app.put("/api/knowledge-bases/{kb_id}", response_model=KnowledgeBaseResponse)
def update_knowledge(
    kb_id: str,
    kb_data: KnowledgeBaseUpdate,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    kb = knowledge_service.update_knowledge_base(db, kb_id, kb_data, user_id=user_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


@app.post("/api/knowledge-bases/{kb_id}/upload")
async def upload_document(
    kb_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    result = await knowledge_service.upload_document(kb_id, file, background_tasks, db, user_id=user_id)
    return result


@app.delete("/api/knowledge-bases/{kb_id}")
def delete_knowledge(kb_id: str, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    kb = knowledge_service.delete_knowledge_base(db, kb_id, user_id=user_id)
    return kb


@app.delete("/api/knowledge-bases/{kb_id}/files/{file_id}")
def delete_file(
    kb_id: str,
    file_id: str,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    kb = knowledge_service.get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    file_record = db.query(KnowledgeFile).filter(
        KnowledgeFile.id == file_id,
        KnowledgeFile.knowledge_base_id == kb_id,
    ).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")

    knowledge_service.delete_knowledge_file(db, kb, file_record)
    return {"message": "文件删除成功"}


@app.post("/api/knowledge-bases/{kb_id}/files/{file_id}/retry")
def retry_file(
    kb_id: str,
    file_id: str,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    kb = knowledge_service.get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    file_record = knowledge_service.retry_knowledge_file(db, kb_id, file_id)
    background_tasks.add_task(
        knowledge_service.process_document_async,
        file_record.id,
        kb_id,
    )
    return {"success": True, "message": "已重新加入处理队列", "file_id": file_record.id}


@app.get("/api/knowledge-bases/{kb_id}/collection-info")
def get_collection_info(kb_id: str, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    kb = knowledge_service.get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id, allow_shared_read=True)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    info = get_document_processor().get_collection_info(kb.collection_name)
    if not info:
        raise HTTPException(status_code=404, detail="向量集合不存在")

    return info


@app.get("/api/knowledge-bases/{kb_id}/files")
def get_knowledge_files(kb_id: str, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    kb, files = knowledge_service.get_knowledge_files_by_kb(db, kb_id=kb_id, user_id=user_id, allow_shared_read=True)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    return files


@app.get("/api/files/{file_id}/preview")
def preview_file(file_id: str, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    try:
        file_record = knowledge_service.get_knowledge_file(db, file_id=file_id, user_id=user_id, allow_shared_read=True)
        if not file_record:
            raise HTTPException(status_code=404, detail="文件不存在")

        full_path = knowledge_service.resolve_upload_path(file_record.file_path)
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="文件不存在或已被删除")

        return FileResponse(
            path=full_path,
            filename=file_record.filename,
            media_type=knowledge_service.get_safe_media_type(file_record.filename),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("文件预览失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="文件预览失败")
