import functools
import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI

load_dotenv()

DEFAULT_MYSQL_USER = "root"
DEFAULT_MYSQL_PASSWORD = "password"
DEFAULT_MYSQL_HOST = "localhost"
DEFAULT_MYSQL_PORT = "3306"
DEFAULT_MYSQL_DATABASE = "aitest_rag"

@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: Optional[str]
    temperature: float
    max_tokens: int
    timeout_connect: float
    timeout_read: float
    timeout_write: float
    timeout_pool: float
    max_retries: int


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    api_key: str
    base_url: Optional[str]
    dimensions: int
    encoding_format: str

@dataclass(frozen=True)
class RetrieverConfig:
    top_k: int
    candidate_k: int
    enable_distance_filter: bool
    distance_threshold: Optional[float]


@dataclass(frozen=True)
class ChromaConfig:
    distance_metric: str


def get_app_env() -> str:
    return os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()


def get_database_url() -> str | None:
    return os.getenv("DATABASE_URL")


def get_mysql_user() -> str:
    return os.getenv("MYSQL_USER", DEFAULT_MYSQL_USER)


def get_mysql_password() -> str:
    return os.getenv("MYSQL_PASSWORD", DEFAULT_MYSQL_PASSWORD)


def get_mysql_host() -> str:
    return os.getenv("MYSQL_HOST", DEFAULT_MYSQL_HOST)


def get_mysql_port() -> str:
    return os.getenv("MYSQL_PORT", DEFAULT_MYSQL_PORT)


def get_mysql_database() -> str:
    return os.getenv("MYSQL_DATABASE", DEFAULT_MYSQL_DATABASE)


def get_session_secret_key() -> str | None:
    return os.getenv("SESSION_SECRET_KEY")


def get_rag_db_path() -> str:
    return os.getenv("RAG_DB_PATH", "./chroma_db")


def get_upload_dir() -> str:
    return os.getenv("UPLOAD_DIR", "./uploads")


def get_temp_upload_dir() -> str:
    return os.getenv("TEMP_UPLOAD_DIR", "./temp_uploads")


@functools.lru_cache(maxsize=1)
def get_llm_config() -> LLMConfig:
    return LLMConfig(
        provider=os.getenv("LLM_PROVIDER", "deepseek"),
        model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_BASE_URL") or None,
        temperature=float(
            os.getenv("LLM_TEMPERATURE", "0.7")
        ),
        max_tokens=int(
            os.getenv("LLM_MAX_TOKENS", "4096")
        ),
        timeout_connect=float(
            os.getenv("LLM_TIMEOUT_CONNECT", "10")
        ),
        timeout_read=float(
            os.getenv("LLM_TIMEOUT_READ", "120")
        ),
        timeout_write=float(
            os.getenv("LLM_TIMEOUT_WRITE", "30")
        ),
        timeout_pool=float(
            os.getenv("LLM_TIMEOUT_POOL", "10")
        ),
        max_retries=int(
            os.getenv("LLM_MAX_RETRIES", "2")
        ),
    )

@functools.lru_cache(maxsize=1)
def get_embedding_config() -> EmbeddingConfig:
    return EmbeddingConfig(
        provider=os.getenv(
            "EMBEDDING_PROVIDER",
            "openai",
        ),
        model=os.getenv(
            "EMBEDDING_MODEL",
            "text-embedding-v4",
        ),
        api_key=os.getenv("EMBEDDING_API_KEY", ""),
        base_url=os.getenv("EMBEDDING_BASE_URL") or None,
        dimensions=int(
            os.getenv(
                "EMBEDDING_DIMENSIONS",
                "1024",
            )
        ),
        encoding_format=os.getenv(
            "EMBEDDING_ENCODING_FORMAT",
            "float",
        ),
    )

@functools.lru_cache(maxsize=1)
def get_retriever_config() -> RetrieverConfig:
    threshold = os.getenv("RETRIEVER_DISTANCE_THRESHOLD")

    return RetrieverConfig(
        top_k=int(os.getenv("RETRIEVER_TOP_K", "5")),
        candidate_k=int(os.getenv("RETRIEVER_CANDIDATE_K", "10")),
        enable_distance_filter=os.getenv(
            "RETRIEVER_ENABLE_DISTANCE_FILTER",
            "false",
        ).lower() == "true",
        distance_threshold=float(threshold)
        if threshold
        else None,
    )

@functools.lru_cache(maxsize=1)
def get_chroma_config() -> ChromaConfig:
    return ChromaConfig(
        distance_metric=os.getenv(
            "CHROMA_DISTANCE_METRIC",
            "l2",
        )
    )

@functools.lru_cache(maxsize=1)
def get_embedding_client() -> OpenAI:
    config = get_embedding_config()

    return OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
    )


@functools.lru_cache(maxsize=1)
def get_async_embedding_client() -> AsyncOpenAI:
    """异步 embedding 客户端，供 asyncio 环境下的检索器使用，避免阻塞事件循环"""
    config = get_embedding_config()

    return AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
    )

