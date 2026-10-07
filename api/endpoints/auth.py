from pathlib import Path
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from models.database import get_db
from services.auth_service import AuthService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))

# 登录失败限流（进程内滑动窗口）：单 worker 部署形态下的最简防爆破；
# 键为 用户名小写+客户端IP，窗口内失败达上限后该键的后续尝试一律 429
_LOGIN_FAIL_WINDOW_SECONDS = 300
_LOGIN_FAIL_LIMIT = 5
_LOGIN_FAIL_MAX_KEYS = 10_000  # 防止键集合被构造性撑爆
_login_failures: dict = defaultdict(deque)
_login_failures_lock = Lock()


def _login_rate_key(request: Request, username: str) -> str:
    client_host = request.client.host if request.client else "unknown"
    return f"{username.strip().lower()}|{client_host}"


def _prune_expired(key: str, now: float) -> deque:
    window = _login_failures[key]
    while window and now - window[0] > _LOGIN_FAIL_WINDOW_SECONDS:
        window.popleft()
    return window


def _login_is_blocked(key: str) -> bool:
    with _login_failures_lock:
        if key not in _login_failures:
            return False
        return len(_prune_expired(key, time.monotonic())) >= _LOGIN_FAIL_LIMIT


def _register_login_failure(key: str) -> None:
    now = time.monotonic()
    with _login_failures_lock:
        if len(_login_failures) >= _LOGIN_FAIL_MAX_KEYS:
            _login_failures.clear()
        _prune_expired(key, now).append(now)


def _clear_login_failures(key: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(key, None)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html")


@router.post("/register")
def register_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    register_result = AuthService.create_user(db, username, password)
    if not register_result["success"]:
        return JSONResponse(
            {"detail": register_result["error"]},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.get("/", response_class=HTMLResponse)
def main_page(request: Request):
    username = request.session.get("username")
    if username is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "用户会话已失效，请重新登录"},
        )
    return RedirectResponse(url="/chat", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/login")
def login_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    rate_key = _login_rate_key(request, username)
    if _login_is_blocked(rate_key):
        return JSONResponse(
            {"detail": "登录尝试过于频繁，请稍后再试"},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    auth_result = AuthService.login_user(db, username, password)
    if not auth_result["success"]:
        _register_login_failure(rate_key)
        return JSONResponse(
            {"detail": "用户名或密码错误"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    _clear_login_failures(rate_key)

    request.session["user_id"] = auth_result["user_id"]
    request.session["username"] = auth_result["username"]
    request.session["login_time"] = auth_result["login_time"]

    response = RedirectResponse(url="/chat", status_code=status.HTTP_303_SEE_OTHER)
    return response


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login?logout=true", status_code=status.HTTP_303_SEE_OTHER)
