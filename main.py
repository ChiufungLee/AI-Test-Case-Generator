from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from api.api_v1 import api_router
from api.endpoints import auth, chat, knowledge_api, testbench_api, workflow_api
from config import get_app_env, get_session_secret_key
from models.database import init_db
from services import knowledge_service, test_run_service

BASE_DIR = Path(__file__).resolve().parent

# 日志配置在创建应用之前完成，保证 create_app 期间（如生产配置校验）的日志格式统一
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def _get_session_secret() -> str:
    session_secret = get_session_secret_key()
    if session_secret:
        return session_secret

    if get_app_env() == "production":
        raise RuntimeError("SESSION_SECRET_KEY must be set in production")

    return "dev-session-secret-change-me"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 后台处理任务不跨进程存活：启动时把卡在 pending/processing 的文件重置为失败，等待用户重试
    knowledge_service.reset_stale_processing_files()
    # 存量执行结果快照可能含明文凭据（写侧脱敏上线前落库），启动时幂等清洗
    test_run_service.redact_stored_request_headers()
    yield



def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_get_session_secret(),
        # 生产（HTTPS）下会话 cookie 不得在明文连接上传输
        https_only=get_app_env() == "production",
    )

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.include_router(api_router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(knowledge_api.router)
    app.include_router(workflow_api.router)
    app.include_router(testbench_api.router)
    return app


app = create_app()
