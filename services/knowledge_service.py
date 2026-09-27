from contextlib import contextmanager
from datetime import datetime
import asyncio
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from models.chat import Conversation
from models.database import create_session
from models.knowledge_models import KnowledgeBase, KnowledgeFile
from sqlalchemy import func, or_
from utils.file_handle import get_document_processor, get_temp_upload_dir, get_upload_dir
from utils.retriever import ChromaRetriever

logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 50 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".pdf"}


def _refresh_kb_file_count(db, kb_id: str) -> int:
    # SessionLocal 为 autoflush=False，需先 flush 让挂起的 INSERT/DELETE 对 COUNT 可见
    db.flush()
    new_count = db.query(KnowledgeFile).filter(KnowledgeFile.knowledge_base_id == kb_id).count()
    db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).update({
        "file_count": new_count,
        "updated_at": func.now(),
    })
    return new_count



def get_upload_root() -> Path:
    return Path(get_upload_dir()).resolve()



def resolve_upload_path(file_path: str) -> Path:
    upload_root = get_upload_root()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = (upload_root / candidate.name).resolve()
    else:
        candidate = candidate.resolve()

    try:
        candidate.relative_to(upload_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="文件路径非法") from exc

    return candidate


async def create_knowledge_record(db, record_data, owner_user_id: int):
    visibility = getattr(record_data, 'visibility', 'private')
    collection_name = f"kb_{uuid.uuid4().hex[:16]}"
    kb = KnowledgeBase(
        name=record_data.name,
        description=record_data.description,
        collection_name=collection_name,
        owner_user_id=owner_user_id,
        visibility=visibility,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return {
        "success": True,
        "message": "知识库创建成功",
        "knowledge_base": kb,
    }


async def get_all_knowledge(db, user_id: int):
    return (
        db.query(KnowledgeBase)
        .filter(or_(KnowledgeBase.owner_user_id == user_id, KnowledgeBase.visibility == "shared"))
        .all()
    )


async def get_knowledge_base_by_id(kb_id, db, user_id: int, allow_shared_read: bool = False):
    if not kb_id:
        return None

    query = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id)
    if allow_shared_read:
        query = query.filter(
            or_(KnowledgeBase.owner_user_id == user_id, KnowledgeBase.visibility == "shared")
        )
    else:
        query = query.filter(KnowledgeBase.owner_user_id == user_id)
    return query.first()


async def update_knowledge_base(db, kb_id, kb_data, user_id: int):
    kb = await get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id)
    if not kb:
        return None

    if kb_data.name is not None:
        kb.name = kb_data.name
    if kb_data.description is not None:
        kb.description = kb_data.description
    if kb_data.visibility is not None:
        kb.visibility = kb_data.visibility

    db.commit()
    db.refresh(kb)
    return kb


def _save_upload_to_disk(file, save_path: Path) -> int:
    """分块写盘并校验大小上限，返回实际字节数（阻塞 I/O，仅供线程池调用）"""
    save_path.parent.mkdir(parents=True, exist_ok=True)
    total_size = 0
    with open(save_path, "wb") as buffer:
        while chunk := file.file.read(1024 * 1024):
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE:
                raise HTTPException(status_code=400, detail="文件大小不能超过 50MB")
            buffer.write(chunk)
    return total_size


async def upload_document(kb_id, file, background_tasks, db, user_id: int):
    kb = await get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持上传 PDF 文件")

    unique_filename = f"{uuid.uuid4().hex}{file_ext}"
    save_path = (get_upload_root() / unique_filename).resolve()

    try:
        total_size = await run_in_threadpool(_save_upload_to_disk, file, save_path)

        if total_size == 0:
            raise HTTPException(status_code=400, detail="文件内容不能为空")

        file_size = total_size
        file_type = (file_ext.lstrip(".") or (file.content_type or "unknown").split("/")[-1]).lower()

        file_record = KnowledgeFile(
            knowledge_base_id=kb_id,
            filename=file.filename,
            file_path=str(save_path),
            file_size=file_size,
            file_type=file_type,
            status="pending",
        )

        db.add(file_record)
        db.commit()
        _refresh_kb_file_count(db, kb_id)
        db.commit()
        db.refresh(file_record)

        background_tasks.add_task(
            process_document_async,
            file_record.id,
            kb_id,
        )

        return {
            "success": True,
            "message": "文件上传成功，正在后台处理",
            "file_id": file_record.id,
            "filename": file.filename,
        }

    except HTTPException:
        if save_path.exists():
            save_path.unlink()
        raise
    except Exception as e:
        if save_path.exists():
            save_path.unlink()
        logger.error("文件上传失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="文件上传失败") from e


async def delete_knowledge_file(db, kb: KnowledgeBase, file_record: KnowledgeFile):
    file_path = resolve_upload_path(file_record.file_path)
    document_processor = get_document_processor()
    # 先删向量：若 DB 提交失败，残留分片会污染检索结果，因此向量删除必须先行
    document_processor.delete_documents_by_file_id(kb.collection_name, file_record.id)

    db.delete(file_record)
    _refresh_kb_file_count(db, kb.id)
    db.commit()

    # 物理文件最后删除：即使失败也只是留下无害的孤儿文件
    try:
        if file_path.exists():
            file_path.unlink()
    except OSError as e:
        logger.warning("删除物理文件失败: %s, %s", file_path, e)


async def delete_knowledge_base(db, kb_id, user_id: int):
    kb = await get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    collection_name = kb.collection_name
    file_paths = []
    for file_record in list(kb.files):
        try:
            file_paths.append(resolve_upload_path(file_record.file_path))
        except HTTPException:
            logger.warning("文件路径非法，跳过物理删除: %s", file_record.file_path)

    try:
        # 先提交 DB：引用该知识库的对话置空并删除记录
        db.query(Conversation).filter(Conversation.knowledge_base_id == kb_id).update(
            {Conversation.knowledge_base_id: None}, synchronize_session=False
        )
        db.delete(kb)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("删除知识库失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="删除知识库失败") from e

    # DB 提交成功后再清理外部资源；失败只记日志（残留的向量集合/文件不再被引用，无害）
    get_document_processor().delete_collection(collection_name)
    await ChromaRetriever.clear_retriever_cache(kb_id)
    for file_path in file_paths:
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError as e:
            logger.warning("删除物理文件失败: %s, %s", file_path, e)

    return {
        "success": True,
        "message": "知识库删除成功",
    }


async def get_knowledge_file(db, file_id: str, user_id: int, allow_shared_read: bool = False):
    query = (
        db.query(KnowledgeFile)
        .join(KnowledgeBase, KnowledgeFile.knowledge_base_id == KnowledgeBase.id)
        .filter(KnowledgeFile.id == file_id)
    )
    if allow_shared_read:
        query = query.filter(
            or_(KnowledgeBase.owner_user_id == user_id, KnowledgeBase.visibility == "shared")
        )
    else:
        query = query.filter(KnowledgeBase.owner_user_id == user_id)
    return query.first()


async def get_knowledge_files_by_kb(db, kb_id: str, user_id: int, allow_shared_read: bool = False):
    kb = await get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id, allow_shared_read=allow_shared_read)
    if not kb:
        return None, None

    files = (
        db.query(KnowledgeFile)
        .filter(KnowledgeFile.knowledge_base_id == kb_id)
        .order_by(KnowledgeFile.uploaded_at.desc())
        .all()
    )
    return kb, files


@contextmanager
def get_db_context():
    """用于后台任务的数据库会话上下文管理器"""
    db = create_session()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



def process_document_async(file_id: str, kb_id: str):
    """后台处理文档（向量化）"""
    with get_db_context() as db:
        try:
            file_record = (
                db.query(KnowledgeFile)
                .filter(KnowledgeFile.id == file_id, KnowledgeFile.knowledge_base_id == kb_id)
                .first()
            )

            if not file_record:
                logger.error("文件记录不存在: %s", file_id)
                return

            file_record.status = "processing"
            db.commit()

            file_path = resolve_upload_path(file_record.file_path)
            if not file_path.exists():
                logger.warning("文件已不存在，跳过后台处理: %s", file_path)
                db.delete(file_record)
                db.commit()
                return

            document_processor = get_document_processor()
            docs = document_processor.load_pdf(str(file_path))
            splits = document_processor.split_documents(docs)

            file_metadata = {
                "file_id": file_record.id,
                "filename": file_record.filename,
                "knowledge_base_id": kb_id,
                "processed_at": datetime.now().isoformat(),
            }

            kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
            if not kb:
                raise Exception("知识库不存在")

            chunk_count = document_processor.save_to_chroma(
                splits=splits,
                collection_name=kb.collection_name,
                file_metadata=file_metadata,
            )

            if splits and chunk_count == 0:
                # 有分片但全部向量化失败：标记失败而不是 completed，避免内容残缺无人知晓
                raise RuntimeError("所有分片向量化失败，未能写入向量库")

            file_record.status = "completed"
            file_record.chunk_count = chunk_count
            file_record.processed_at = func.now()
            total_file_count = _refresh_kb_file_count(db, kb_id)
            db.commit()
            logger.info("文档处理完成: %s, 分片数: %s", file_record.filename, chunk_count)
            logger.info("知识库 %s 当前文件总数: %s", kb_id, total_file_count)

        except Exception as e:
            db.rollback()
            refreshed_record = (
                db.query(KnowledgeFile)
                .filter(KnowledgeFile.id == file_id, KnowledgeFile.knowledge_base_id == kb_id)
                .first()
            )

            if isinstance(e, FileNotFoundError):
                if refreshed_record is not None:
                    db.delete(refreshed_record)
                    _refresh_kb_file_count(db, kb_id)
                    db.commit()
                logger.warning("文件在后台处理期间已被删除: %s", file_id)
                return

            if refreshed_record is not None:
                refreshed_record.status = "failed"
                db.commit()
            logger.error("文档处理失败: %s", e, exc_info=True)
            return



def get_safe_media_type(filename: str) -> str:
    media_type, _ = mimetypes.guess_type(filename)
    return media_type or "application/octet-stream"


def _validate_attachment(file) -> str:
    """校验聊天附件的文件名与类型，返回小写扩展名"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持上传 PDF 文件")
    return file_ext


async def save_chat_attachment(db, file, kb: KnowledgeBase) -> KnowledgeFile:
    """聊天附带文档并入知识库：校验、存盘、登记并同步向量化（阻塞部分经线程池执行）。

    向量化失败时文件记录保留为 failed 状态（与知识库页面上传的行为一致，可在知识库中管理），
    并向调用方抛出 400。
    """
    file_ext = _validate_attachment(file)
    unique_filename = f"{uuid.uuid4().hex}{file_ext}"
    save_path = (get_upload_root() / unique_filename).resolve()

    try:
        total_size = await run_in_threadpool(_save_upload_to_disk, file, save_path)
    except Exception:
        if save_path.exists():
            save_path.unlink()
        raise

    if total_size == 0:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件内容不能为空")

    file_record = KnowledgeFile(
        knowledge_base_id=kb.id,
        filename=file.filename,
        file_path=str(save_path),
        file_size=total_size,
        file_type=file_ext.lstrip("."),
        status="processing",
    )
    db.add(file_record)
    db.commit()
    db.refresh(file_record)
    _refresh_kb_file_count(db, kb.id)
    db.commit()

    await asyncio.to_thread(process_document_async, file_record.id, kb.id)

    db.expire_all()
    final_record = db.query(KnowledgeFile).filter(KnowledgeFile.id == file_record.id).first()
    if not final_record or final_record.status != "completed":
        raise HTTPException(status_code=400, detail="文档处理失败，请检查文件内容后重试")

    return final_record


async def extract_pdf_text(file) -> str:
    """聊天附带文档（无知识库）的纯文本提取：存临时文件→解析→立即删除临时文件。

    不产生向量与数据库记录；解析不出内容时返回空字符串，由调用方处理。
    """
    file_ext = _validate_attachment(file)
    temp_path = (Path(get_temp_upload_dir()).resolve() / f"{uuid.uuid4().hex}{file_ext}")

    try:
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        total_size = await run_in_threadpool(_save_upload_to_disk, file, temp_path)
        if total_size == 0:
            raise HTTPException(status_code=400, detail="文件内容不能为空")

        docs = await asyncio.to_thread(get_document_processor().load_pdf, str(temp_path))
    finally:
        temp_path.unlink(missing_ok=True)

    return "\n\n".join(doc.page_content for doc in docs if doc.page_content.strip())
