import logging
from urllib.parse import quote_plus

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import close_all_sessions, declarative_base, sessionmaker

from config import (
    DEFAULT_MYSQL_PASSWORD,
    get_app_env,
    get_database_url,
    get_mysql_database,
    get_mysql_host,
    get_mysql_password,
    get_mysql_port,
    get_mysql_user,
)

logger = logging.getLogger(__name__)

Base = declarative_base()
SessionLocal = sessionmaker(autocommit=False, autoflush=False)
engine: Engine | None = None



def build_database_url() -> str:
    database_url = get_database_url()
    if database_url:
        return database_url

    mysql_user = get_mysql_user()
    mysql_password = get_mysql_password()
    mysql_host = get_mysql_host()
    mysql_port = get_mysql_port()
    mysql_database = get_mysql_database()

    if get_app_env() == "production" and mysql_password == DEFAULT_MYSQL_PASSWORD:
        raise RuntimeError("Set DATABASE_URL or MYSQL_PASSWORD before starting in production")

    return (
        f"mysql+pymysql://{quote_plus(mysql_user)}:{quote_plus(mysql_password)}"
        f"@{mysql_host}:{mysql_port}/{mysql_database}"
    )



def _create_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            echo=False,
        )

    return create_engine(
        database_url,
        pool_size=20,
        max_overflow=30,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args={
            "charset": "utf8mb4",
            "connect_timeout": 10,
        },
        echo=False,
    )



def configure_database(database_url: str | None = None) -> Engine:
    global engine

    engine = _create_engine(database_url or build_database_url())
    SessionLocal.configure(bind=engine)
    return engine



def get_engine() -> Engine:
    if engine is None:
        return configure_database()
    return engine



def create_session():
    get_engine()
    return SessionLocal()



def _ensure_schema_updates(current_engine: Engine):
    """补齐当前版本需要的轻量 schema 变更。"""
    inspector = inspect(current_engine)
    table_names = set(inspector.get_table_names())

    if "knowledge_bases" not in table_names:
        return

    column_names = {column["name"] for column in inspector.get_columns("knowledge_bases")}
    index_names = {index["name"] for index in inspector.get_indexes("knowledge_bases")}

    with current_engine.begin() as conn:
        if "owner_user_id" not in column_names:
            conn.execute(text("ALTER TABLE knowledge_bases ADD COLUMN owner_user_id INTEGER NULL"))

        if "ix_knowledge_bases_owner_user_id" not in index_names:
            conn.execute(
                text("CREATE INDEX ix_knowledge_bases_owner_user_id ON knowledge_bases (owner_user_id)")
            )

    if "users" in table_names:
        user_columns = {c["name"] for c in inspector.get_columns("users")}
        with current_engine.begin() as conn:
            if "created_at" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"))
            if "last_login_at" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN last_login_at DATETIME NULL"))

    if "messages" in table_names:
        message_columns = {c["name"] for c in inspector.get_columns("messages")}
        with current_engine.begin() as conn:
            # 消息附件改为独立字段存储，不再拼接进 content
            if "attachment_name" not in message_columns:
                conn.execute(text("ALTER TABLE messages ADD COLUMN attachment_name VARCHAR(255) NULL"))
            if "attachment_text" not in message_columns:
                # MEDIUMTEXT 容纳较长文档正文；SQLite 中含 TEXT 的类型名即 TEXT 亲和
                conn.execute(text("ALTER TABLE messages ADD COLUMN attachment_text MEDIUMTEXT NULL"))

    if "knowledge_files" in table_names:
        file_columns = {c["name"] for c in inspector.get_columns("knowledge_files")}
        with current_engine.begin() as conn:
            if "content_hash" not in file_columns:
                conn.execute(text("ALTER TABLE knowledge_files ADD COLUMN content_hash VARCHAR(64) NULL"))
                conn.execute(
                    text("CREATE INDEX ix_knowledge_files_content_hash ON knowledge_files (content_hash)")
                )
            if "skipped_chunks" not in file_columns:
                conn.execute(text("ALTER TABLE knowledge_files ADD COLUMN skipped_chunks INTEGER NULL"))
            if "error" not in file_columns:
                conn.execute(text("ALTER TABLE knowledge_files ADD COLUMN error TEXT NULL"))
            # 幂等回填：存量行的 NULL 会让响应模型校验失败（skipped_chunks 期望 int）。
            # 放在 ALTER 之后无条件执行——无论列是刚补的还是早已存在，NULL 都要归零
            conn.execute(text("UPDATE knowledge_files SET skipped_chunks = 0 WHERE skipped_chunks IS NULL"))

    if "test_runs" in table_names:
        run_columns = {c["name"] for c in inspector.get_columns("test_runs")}
        with current_engine.begin() as conn:
            if "endpoints_json" not in run_columns:
                conn.execute(text("ALTER TABLE test_runs ADD COLUMN endpoints_json TEXT NULL"))
            # 幂等回填：存量执行记录无接口快照，置空数组供历史渲染
            conn.execute(text("UPDATE test_runs SET endpoints_json = '[]' WHERE endpoints_json IS NULL"))



def init_db():
    """初始化数据库（创建表结构并补齐必要字段）"""
    current_engine = get_engine()
    Base.metadata.create_all(bind=current_engine)
    _ensure_schema_updates(current_engine)



def get_db():
    db = create_session()
    try:
        yield db
    finally:
        db.close()



def check_database_connection():
    """检查数据库连接是否正常"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("数据库连接失败: %s", e, exc_info=True)
        return False

