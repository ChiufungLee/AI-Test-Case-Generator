from contextlib import contextmanager
from datetime import datetime
import asyncio
import hashlib
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

# 部分分片向量化失败时，跳过比例达到该阈值即标记文件 failed（索引残缺不可信）；
# 低于阈值仍标记 completed，但把 skipped_chunks 记录下来供前端提示
EMBED_PARTIAL_FAILURE_RATIO = 0.2

# 服务重启时处于这些状态的文件不可能再有后台任务在跑，重置为失败等待重试
STALE_PROCESSING_STATUSES = ("pending", "processing")


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


def create_knowledge_record(db, record_data, owner_user_id: int):
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


def get_all_knowledge(db, user_id: int):
    return (
        db.query(KnowledgeBase)
        .filter(or_(KnowledgeBase.owner_user_id == user_id, KnowledgeBase.visibility == "shared"))
        .all()
    )


def get_knowledge_base_by_id(kb_id, db, user_id: int, allow_shared_read: bool = False):
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


def update_knowledge_base(db, kb_id, kb_data, user_id: int):
    kb = get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id)
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


def _save_upload_to_disk(file, save_path: Path) -> tuple[int, str]:
    """分块写盘并校验大小上限，返回 (实际字节数, 内容 SHA-256)（阻塞 I/O，仅供线程池调用）"""
    save_path.parent.mkdir(parents=True, exist_ok=True)
    total_size = 0
    digest = hashlib.sha256()
    with open(save_path, "wb") as buffer:
        while chunk := file.file.read(1024 * 1024):
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE:
                raise HTTPException(status_code=400, detail="文件大小不能超过 50MB")
            digest.update(chunk)
            buffer.write(chunk)
    return total_size, digest.hexdigest()


def _ensure_no_duplicate_file(db, kb_id: str, content_hash: str):
    """同一知识库内按内容哈希去重：已有同内容且未失败的文件时拒绝重复入库。

    失败状态的文件允许重新上传（相当于替代手动重试）。
    """
    existing = (
        db.query(KnowledgeFile)
        .filter(
            KnowledgeFile.knowledge_base_id == kb_id,
            KnowledgeFile.content_hash == content_hash,
            KnowledgeFile.status != "failed",
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"知识库中已存在相同内容的文件《{existing.filename}》，无需重复上传",
        )


async def upload_document(kb_id, file, background_tasks, db, user_id: int):
    kb = await asyncio.to_thread(
        get_knowledge_base_by_id, kb_id=kb_id, db=db, user_id=user_id
    )
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
        total_size, content_hash = await run_in_threadpool(_save_upload_to_disk, file, save_path)

        if total_size == 0:
            raise HTTPException(status_code=400, detail="文件内容不能为空")

        # 内容哈希去重（校验在写盘之后，需先清理已落盘文件再拒绝）
        _ensure_no_duplicate_file(db, kb_id, content_hash)

        file_size = total_size
        file_type = (file_ext.lstrip(".") or (file.content_type or "unknown").split("/")[-1]).lower()

        file_record = KnowledgeFile(
            knowledge_base_id=kb_id,
            filename=file.filename,
            file_path=save_path.name,
            file_size=file_size,
            file_type=file_type,
            status="pending",
            content_hash=content_hash,
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


def delete_knowledge_file(db, kb: KnowledgeBase, file_record: KnowledgeFile):
    file_path = resolve_upload_path(file_record.file_path)
    document_processor = get_document_processor()
    # 先删向量：若删除失败仍删 DB 记录，残留分片会继续参与检索，因此失败必须中止
    vector_deleted = document_processor.delete_documents_by_file_id(kb.collection_name, file_record.id)
    if not vector_deleted:
        raise HTTPException(status_code=500, detail="向量删除失败，请稍后重试")

    db.delete(file_record)
    _refresh_kb_file_count(db, kb.id)
    db.commit()

    # 物理文件最后删除：即使失败也只是留下无害的孤儿文件
    try:
        if file_path.exists():
            file_path.unlink()
    except OSError as e:
        logger.warning("删除物理文件失败: %s, %s", file_path, e)


def delete_knowledge_base(db, kb_id, user_id: int):
    kb = get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id)
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
    ChromaRetriever.clear_retriever_cache(kb_id)
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


def get_knowledge_file(db, file_id: str, user_id: int, allow_shared_read: bool = False):
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


def get_knowledge_files_by_kb(db, kb_id: str, user_id: int, allow_shared_read: bool = False):
    kb = get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id, allow_shared_read=allow_shared_read)
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
            # load_pdf 内已完成结构感知分块，直接产出最终分块
            splits = document_processor.load_pdf(str(file_path))

            file_metadata = {
                "file_id": file_record.id,
                "filename": file_record.filename,
                "knowledge_base_id": kb_id,
                "processed_at": datetime.now().isoformat(),
            }

            kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
            if not kb:
                raise Exception("知识库不存在")

            chunk_count, skipped_chunks = document_processor.save_to_chroma(
                splits=splits,
                collection_name=kb.collection_name,
                file_metadata=file_metadata,
            )

            if splits and chunk_count == 0:
                # 有分片但全部向量化失败：标记失败而不是 completed，避免内容残缺无人知晓
                raise RuntimeError("所有分片向量化失败，未能写入向量库")

            total_chunks = chunk_count + skipped_chunks
            file_record.skipped_chunks = skipped_chunks
            if skipped_chunks and skipped_chunks / total_chunks >= EMBED_PARTIAL_FAILURE_RATIO:
                # 索引残缺超过阈值：标记失败让用户重试，而不是假装完整
                file_record.status = "failed"
                file_record.error = (
                    f"{skipped_chunks}/{total_chunks} 个分片向量化失败，索引不完整，请重试处理"
                )
                db.commit()
                logger.error(
                    "文档部分向量化失败达阈值: %s, 成功 %d / 跳过 %d",
                    file_record.filename, chunk_count, skipped_chunks,
                )
                return

            file_record.status = "completed"
            file_record.chunk_count = chunk_count
            file_record.processed_at = func.now()
            if skipped_chunks:
                file_record.error = f"{skipped_chunks}/{total_chunks} 个分片向量化失败被跳过"
            total_file_count = _refresh_kb_file_count(db, kb_id)
            db.commit()
            logger.info("文档处理完成: %s, 分片数: %s, 跳过: %s", file_record.filename, chunk_count, skipped_chunks)
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
                refreshed_record.error = str(e)[:500]
                db.commit()
            logger.error("文档处理失败: %s", e, exc_info=True)
            return



def reset_stale_processing_files() -> int:
    """应用启动时把卡在 pending/processing 的文件重置为 failed。

    后台任务不跨进程存活，重启后这些状态永远等不到处理结果；
    重置为失败后可经重试端点重新入队。返回重置的文件数。
    """
    db = create_session()
    try:
        stale = (
            db.query(KnowledgeFile)
            .filter(KnowledgeFile.status.in_(STALE_PROCESSING_STATUSES))
            .all()
        )
        if not stale:
            return 0
        for record in stale:
            record.status = "failed"
            record.error = "处理因服务重启中断，请重试"
        db.commit()
        logger.warning("已重置 %d 个因服务重启而卡住的文件记录", len(stale))
        return len(stale)
    except Exception as e:
        db.rollback()
        logger.error("重置卡住的文件记录失败: %s", e, exc_info=True)
        return 0
    finally:
        db.close()


def retry_knowledge_file(db, kb_id: str, file_id: str):
    """重新处理失败/中断的文件：清理上次可能写入的部分向量后重新入队。"""
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    file_record = (
        db.query(KnowledgeFile)
        .filter(KnowledgeFile.id == file_id, KnowledgeFile.knowledge_base_id == kb_id)
        .first()
    )
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")

    if file_record.status in STALE_PROCESSING_STATUSES:
        raise HTTPException(status_code=400, detail="文件正在处理中，请稍后")
    if file_record.status == "completed":
        raise HTTPException(status_code=400, detail="文件已处理完成，无需重试")

    file_path = resolve_upload_path(file_record.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=400, detail="文件已不存在，无法重新处理")

    # 上次失败可能已写入部分向量，重新处理前先清掉，避免同内容分片重复
    get_document_processor().delete_documents_by_file_id(kb.collection_name, file_id)

    file_record.status = "pending"
    file_record.error = None
    file_record.skipped_chunks = 0
    db.commit()
    db.refresh(file_record)

    return file_record


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


async def register_chat_attachment(db, file, kb_id: str, user_id: int) -> KnowledgeFile:
    """聊天附带文档并入知识库：校验、存盘、内容哈希去重，登记为 pending 后立即返回。

    向量化由调用方安排后台任务执行——大 PDF 的解析与向量化不应阻塞聊天请求；
    文档就绪前检索不到其内容，完成后可再次提问。
    """
    file_ext = _validate_attachment(file)
    kb = get_knowledge_base_by_id(kb_id=kb_id, db=db, user_id=user_id, allow_shared_read=True)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="共享知识库仅属主可附带文档入库")

    unique_filename = f"{uuid.uuid4().hex}{file_ext}"
    save_path = (get_upload_root() / unique_filename).resolve()

    try:
        total_size, content_hash = await run_in_threadpool(_save_upload_to_disk, file, save_path)
    except Exception:
        if save_path.exists():
            save_path.unlink()
        raise

    if total_size == 0:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件内容不能为空")

    _ensure_no_duplicate_file(db, kb.id, content_hash)

    file_record = KnowledgeFile(
        knowledge_base_id=kb.id,
        filename=file.filename,
        file_path=save_path.name,
        file_size=total_size,
        file_type=file_ext.lstrip("."),
        status="pending",
        content_hash=content_hash,
    )
    db.add(file_record)
    db.commit()
    db.refresh(file_record)
    _refresh_kb_file_count(db, kb.id)
    db.commit()
    return file_record


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
