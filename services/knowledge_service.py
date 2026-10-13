from contextlib import contextmanager
from datetime import datetime
import asyncio
import concurrent.futures
import hashlib
import logging
import mimetypes
import os
from pathlib import Path
import re
import unicodedata
import uuid

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from models.chat import Conversation
from models.database import create_session
from models.knowledge_models import KnowledgeBase, KnowledgeFile
from sqlalchemy import func, or_
from utils.data_handle import to_markdown_table
from utils.file_handle import get_document_processor, get_temp_upload_dir, get_upload_dir
from utils.pdf_layout import extract_pdf_text_with_layout
from utils.retriever import ChromaRetriever

logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 50 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".pdf"}

# 需求文档（测试任务「需求原文」上传）：只做纯文本提取、不进向量库，
# 因此比知识库放宽到 Word/TXT/Markdown；.doc（二进制旧格式）不支持
REQUIREMENT_DOC_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
# 纯文本上限：对齐 WorkflowCreate.requirement_text 的 50000 字符校验上限，
# 超出即截断并回报 truncated，避免前端回填后创建任务被 422 拒绝
MAX_REQUIREMENT_DOC_CHARS = 50_000

# 提取结果中的连续空行（3 个以上换行）压缩为单个空行
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

# 无字形字符（浏览器只能画成方框/豆腐块）：零宽与双向控制符
_INVISIBLE_FORMAT_CHARS = frozenset(
    {"\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"}
)
# 语义上是换行、但浏览器/文本框里常显示成方框的字符
_UNICODE_LINE_SEPARATORS = ("\u2028", "\u2029", "\u0085")

# 部分分片向量化失败时，跳过比例达到该阈值即标记文件 failed（索引残缺不可信）；
# 低于阈值仍标记 completed，但把 skipped_chunks 记录下来供前端提示
EMBED_PARTIAL_FAILURE_RATIO = 0.2

# 服务重启时处于这些状态的文件不可能再有后台任务在跑，重置为失败等待重试
STALE_PROCESSING_STATUSES = ("pending", "processing")

# 文档解析/向量化的专用线程池：PDF 解析与 OCR 是分钟级阻塞操作，
# 与 anyio 请求线程池隔离并限制并发（后台任务在池外等待结果），
# 避免并发上传挤占普通请求的线程容量。进程退出时由解释器 atexit 汇合
_DOC_PROCESS_MAX_WORKERS = 2
_document_pool: concurrent.futures.ThreadPoolExecutor | None = None


def get_document_pool() -> concurrent.futures.ThreadPoolExecutor:
    """惰性创建文档处理线程池（测试经 tests/testing_state 重置）"""
    global _document_pool
    if _document_pool is None:
        _document_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=_DOC_PROCESS_MAX_WORKERS, thread_name_prefix="doc-process"
        )
    return _document_pool


def _process_in_document_pool(file_id: str, kb_id: str) -> None:
    """在专用线程池中执行文档处理并等待完成（阻塞调用，只由背景任务调度）"""
    get_document_pool().submit(process_document_async, file_id, kb_id).result()


def schedule_document_processing(background_tasks, file_id: str, kb_id: str) -> None:
    """把文档向量化调度到专用线程池执行（上传 / 聊天附件 / 重试三个入口共用）"""
    background_tasks.add_task(_process_in_document_pool, file_id, kb_id)


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

        schedule_document_processing(background_tasks, file_record.id, kb_id)

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

    # 上次失败可能已写入部分向量，重新处理前先清掉，避免同内容分片重复；
    # 清理失败必须中止——残留分片会与新分片叠加，检索出现重复内容
    if not get_document_processor().delete_documents_by_file_id(kb.collection_name, file_id):
        raise HTTPException(status_code=500, detail="清理上次残留向量失败，请稍后重试")

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


def _register_attachment_record(
    db, kb: KnowledgeBase, file, file_ext: str, save_path: Path, total_size: int, content_hash: str
) -> KnowledgeFile:
    """登记聊天附件记录（同步 DB 操作，由调用方经 asyncio.to_thread 执行）"""
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


async def register_chat_attachment(db, file, kb_id: str, user_id: int) -> KnowledgeFile:
    """聊天附带文档并入知识库：校验、存盘、内容哈希去重，登记为 pending 后立即返回。

    向量化由调用方安排后台任务执行——大 PDF 的解析与向量化不应阻塞聊天请求；
    文档就绪前检索不到其内容，完成后可再次提问。DB 操作全部经线程池执行，
    不在事件循环内做同步 DB。
    """
    file_ext = _validate_attachment(file)
    kb = await asyncio.to_thread(
        get_knowledge_base_by_id, kb_id=kb_id, db=db, user_id=user_id, allow_shared_read=True
    )
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="共享知识库仅属主可附带文档入库")

    unique_filename = f"{uuid.uuid4().hex}{file_ext}"
    save_path = (get_upload_root() / unique_filename).resolve()

    try:
        total_size, content_hash = await run_in_threadpool(_save_upload_to_disk, file, save_path)
        if total_size == 0:
            raise HTTPException(status_code=400, detail="文件内容不能为空")

        # 去重校验在写盘之后：任何失败（含 409 重复内容）都要清理已落盘文件，
        # 否则 uploads/ 会残留无记录指向的孤儿文件
        return await asyncio.to_thread(
            _register_attachment_record, db, kb, file, file_ext, save_path, total_size, content_hash
        )
    except Exception:
        if save_path.exists():
            save_path.unlink()
        raise


def _load_pdf_text(file_path: str) -> str:
    """PDF 纯文本提取（阻塞，仅供线程池调用）。

    优先用 `utils.pdf_layout` 按字形坐标还原阅读顺序（表格输出为 Markdown 表格）：
    本环境下 unstructured 的 fast 策略实测返回空，PDF 会退到 PyPDFLoader，
    其内容流顺序会把表格拆成"一列一行"。版面还原失败或取不到正文（如纯图片 PDF）
    时回退 DocumentProcessor 的三级加载（含 OCR）。
    """
    try:
        layout_text = extract_pdf_text_with_layout(file_path)
    except Exception as e:
        logger.warning("PDF 版面还原失败，回退 DocumentProcessor 解析: %s", e)
        layout_text = ""

    if layout_text.strip():
        return layout_text

    docs = get_document_processor().load_pdf(file_path)
    return "\n\n".join(doc.page_content for doc in docs if doc.page_content.strip())


async def extract_pdf_text(file) -> str:
    """聊天附带文档（无知识库）的纯文本提取：存临时文件→解析→立即删除临时文件。

    不产生向量与数据库记录；解析不出内容时返回空字符串，由调用方处理。
    """
    file_ext = _validate_attachment(file)
    temp_path = (Path(get_temp_upload_dir()).resolve() / f"{uuid.uuid4().hex}{file_ext}")

    try:
        # _save_upload_to_disk 返回 (字节数, 内容哈希)：这里只需字节数做空文件校验
        total_size, _ = await run_in_threadpool(_save_upload_to_disk, file, temp_path)
        if total_size == 0:
            raise HTTPException(status_code=400, detail="文件内容不能为空")

        return await asyncio.to_thread(_load_pdf_text, str(temp_path))
    finally:
        temp_path.unlink(missing_ok=True)


# ---------- 需求文档纯文本提取（测试任务「需求原文」上传） ----------


def _validate_requirement_document(file) -> str:
    """校验需求文档文件名与类型，返回小写扩展名"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in REQUIREMENT_DOC_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持上传 PDF、Word(.docx)、TXT、Markdown 文档")
    return file_ext


def _decode_plain_text(raw: bytes) -> str:
    """TXT/Markdown 解码：优先 UTF-8（含 BOM），中文需求文档常见 GB18030 兜底"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _iter_docx_blocks(document):
    """按文档流顺序迭代段落与表格；旧版 python-docx 无该 API 时退化为段落 + 表格"""
    if hasattr(document, "iter_inner_content"):
        yield from document.iter_inner_content()
        return
    yield from document.paragraphs
    yield from document.tables


def _extract_docx_text(file_path: str) -> str:
    """docx 纯文本：段落按序取文本，表格渲染为 Markdown 表格（需求文档常把规则放表格里）"""
    from docx import Document as DocxDocument  # 延迟导入：仅 Word 需求文档需要
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = DocxDocument(file_path)
    parts: list[str] = []
    for block in _iter_docx_blocks(document):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                parts.append(text)
        elif isinstance(block, Table):
            rows = [
                [cell.text.strip() for cell in row.cells]
                for row in block.rows
            ]
            markdown = to_markdown_table([row for row in rows if any(row)])
            if markdown:
                parts.append(markdown)
    return "\n".join(parts)


# CJK 部首补充（U+2E80–U+2EFF）在 Unicode 里没有 NFKC 分解，但 PDF 抽取常把"简体字形"
# 落在这段码位（实测需求文档里的 ⻜书、部⻔）。这里按 Unicode 名称逐字给出对应汉字。
_RADICAL_SUPPLEMENT_MAP = {
    0x2EB0: "纟",  # C-SIMPLIFIED SILK
    0x2EC5: "见",  # C-SIMPLIFIED SEE
    0x2EC6: "角",  # SIMPLIFIED HORN
    0x2EC8: "讠",  # C-SIMPLIFIED SPEECH
    0x2EC9: "贝",  # C-SIMPLIFIED SHELL
    0x2ECB: "车",  # C-SIMPLIFIED CART
    0x2ECC: "辶",  # SIMPLIFIED WALK
    0x2ED0: "钅",  # C-SIMPLIFIED GOLD
    0x2ED3: "长",  # C-SIMPLIFIED LONG
    0x2ED4: "门",  # C-SIMPLIFIED GATE
    0x2ED9: "韦",  # C-SIMPLIFIED TANNED LEATHER
    0x2EDA: "页",  # C-SIMPLIFIED LEAF
    0x2EDB: "风",  # C-SIMPLIFIED WIND
    0x2EDC: "飞",  # C-SIMPLIFIED FLY
    0x2EE0: "饣",  # C-SIMPLIFIED EAT
    0x2EE2: "马",  # C-SIMPLIFIED HORSE
    0x2EE5: "鱼",  # C-SIMPLIFIED FISH
    0x2EE6: "鸟",  # C-SIMPLIFIED BIRD
    0x2EE7: "卤",  # C-SIMPLIFIED SALT
    0x2EE8: "麦",  # SIMPLIFIED WHEAT
    0x2EE9: "黄",  # SIMPLIFIED YELLOW
    0x2EEA: "黾",  # C-SIMPLIFIED FROG
    0x2EEB: "齐",  # J-SIMPLIFIED EVEN
    0x2EEC: "齐",  # C-SIMPLIFIED EVEN
    0x2EED: "齿",  # J-SIMPLIFIED TOOTH
    0x2EEE: "齿",  # C-SIMPLIFIED TOOTH
    0x2EEF: "龙",  # J-SIMPLIFIED DRAGON
    0x2EF0: "龙",  # C-SIMPLIFIED DRAGON
    0x2EF2: "龟",  # J-SIMPLIFIED TURTLE
    0x2EF3: "龟",  # C-SIMPLIFIED TURTLE
}

# 康熙部首经 NFKC 归一后，少数落到繁体字形（⼾→戶、⾶→飛、⼀→一 无变化）。
# 这里只映射"由部首归一得到"的繁体字形，不作用于文档里本来就有的繁体正文
_TRADITIONAL_FROM_RADICAL = {
    "戶": "户", "門": "门", "車": "车", "馬": "马", "魚": "鱼", "鳥": "鸟", "頁": "页",
    "風": "风", "飛": "飞", "長": "长", "韋": "韦", "鹵": "卤", "麥": "麦", "黃": "黄",
    "黽": "黾", "齊": "齐", "齒": "齿", "龍": "龙", "龜": "龟", "貝": "贝", "見": "见",
    "靑": "青",
}


def _normalize_cjk_compat(text: str) -> str:
    """把"看着一样、码位不同"的部首字形还原为常规汉字。

    这两类码位是 PDF/Word 文本转换的常见产物：字形与常规汉字几乎一致、码位不同，
    在某些字体下没有字形就会显示成方框（例如 `租⼾` U+2F3E 实为 `租户`）。

    - 康熙部首（U+2F00–U+2FDF）与 CJK 兼容汉字（U+F900–U+FAFF）：走 NFKC 分解；
    - CJK 部首补充（U+2E80–U+2EFF）：Unicode 未给分解，按名称查表映射；
    - 逐字符处理，避免整体 NFKC 顺带把全角标点/数字也改成半角。
    """
    parts: list[str] = []
    for char in text:
        code = ord(char)
        if 0x2F00 <= code <= 0x2FDF or 0xF900 <= code <= 0xFAFF:
            mapped = unicodedata.normalize("NFKC", char)
            parts.append(_TRADITIONAL_FROM_RADICAL.get(mapped, mapped))
        else:
            parts.append(_RADICAL_SUPPLEMENT_MAP.get(code, char))
    return "".join(parts)


def _is_unrenderable_char(ch: str) -> bool:
    """判断是否为"没有字形可渲染"的解析噪声（页面上只会显示成方框）。

    - C0/C1 控制符（换行与制表保留）
    - 私用区码位（Wingdings/Symbol 等符号字体的残留，如 U+E000–U+F8FF）
    - 替换字符 U+FFFD（解析器拿不到字形映射时的占位）
    - 零宽字符与双向控制符（不可见，但会污染提示词与字符串比较）
    """
    if ch in "\n\t":
        return False
    code = ord(ch)
    if code < 0x20 or 0x7F <= code <= 0x9F:
        return True
    if 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0x10FFFD:
        return True
    if ch == "\ufffd" or ch in _INVISIBLE_FORMAT_CHARS:
        return True
    return 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069


def _normalize_extracted_text(text: str) -> tuple[str, int]:
    """统一换行、压缩连续空行、归一兼容汉字并剔除无字形噪声。

    返回 (正文, 被剔除的字符数)；剔除数量回显给用户，让"文档里有解析不出的符号"
    这件事可见，而不是静默丢字。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for separator in _UNICODE_LINE_SEPARATORS:
        normalized = normalized.replace(separator, "\n")

    normalized = _normalize_cjk_compat(normalized).replace("\u00a0", " ")
    cleaned = "".join(ch for ch in normalized if not _is_unrenderable_char(ch))
    return _MULTI_NEWLINE_RE.sub("\n\n", cleaned).strip(), len(normalized) - len(cleaned)


def _extract_requirement_text(file_ext: str, file_path: Path) -> str:
    """按扩展名分发提取器（PDF 走 DocumentProcessor 三级加载，其余为纯文本读取）"""
    if file_ext == ".pdf":
        return _load_pdf_text(str(file_path))
    if file_ext == ".docx":
        return _extract_docx_text(str(file_path))
    return _decode_plain_text(file_path.read_bytes())


async def extract_requirement_document(file) -> tuple[str, bool, int]:
    """测试任务的需求文档 → 需求原文纯文本，返回 (文本, 是否因超长截断, 剔除的噪声字符数)。

    与知识库上传不同：不写持久化文件、不产生数据库与向量记录，
    临时文件在解析后立即删除；解析不出内容时返回空串，由调用方转为 400。
    """
    file_ext = _validate_requirement_document(file)
    temp_path = (Path(get_temp_upload_dir()).resolve() / f"{uuid.uuid4().hex}{file_ext}")

    try:
        total_size, _ = await run_in_threadpool(_save_upload_to_disk, file, temp_path)
        if total_size == 0:
            raise HTTPException(status_code=400, detail="文件内容不能为空")

        try:
            text = await asyncio.to_thread(_extract_requirement_text, file_ext, temp_path)
        except HTTPException:
            raise
        except Exception as e:
            # 损坏/加密/改名（扩展名与实际格式不符）的文档属用户可修的问题：
            # 细节进日志，用户拿到可行动的 400，而不是一个无信息的 500
            logger.error("需求文档解析失败 (%s): %s", file_ext, e, exc_info=True)
            raise HTTPException(
                status_code=400,
                detail="文档解析失败：文件可能已损坏、加密或与扩展名格式不符，请检查后重试",
            ) from e
    finally:
        temp_path.unlink(missing_ok=True)

    text, dropped_chars = _normalize_extracted_text(text or "")
    if len(text) > MAX_REQUIREMENT_DOC_CHARS:
        return text[:MAX_REQUIREMENT_DOC_CHARS], True, dropped_chars
    return text, False, dropped_chars
