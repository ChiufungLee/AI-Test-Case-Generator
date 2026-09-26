from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from api.api_v1 import api_router
from api.endpoints import auth, chat, knowledg_api as kb
from config import get_app_env, get_session_secret_key
from models.database import init_db

BASE_DIR = Path(__file__).resolve().parent



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
    yield



def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=_get_session_secret())

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.include_router(api_router)
    app.include_router(auth.router)
    app.include_router(chat.app)
    app.include_router(kb.app)
    return app


app = create_app()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
