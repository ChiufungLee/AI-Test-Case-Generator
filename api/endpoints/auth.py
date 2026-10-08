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

# 注册输入的边界（与 users 表列宽、bcrypt 的有效输入长度对齐）：
# 前端 required / "至少6位" 只在 UI 生效，直接调 API 必须由服务端兜底——空口令账号
# 可注册且能登录；>72 字节口令在 bcrypt 5.x 抛 ValueError（注册 500），在 4.x 被静默
# 截断到 72 字节（不同口令等效，认证语义被破坏）
MAX_USERNAME_CHARS = 50
MIN_PASSWORD_CHARS = 6
MAX_PASSWORD_BYTES = 72


def _validate_new_credentials(username: str, password: str) -> str | None:
    """注册输入校验：返回错误文案，None 表示通过"""
    if username != username.strip():
        return "用户名不能包含首尾空格"
    if not username:
        return "用户名不能为空"
    if len(username) > MAX_USERNAME_CHARS:
        return f"用户名不能超过 {MAX_USERNAME_CHARS} 个字符"
    if not password:
        return "密码不能为空"
    if len(password) < MIN_PASSWORD_CHARS:
        return f"密码至少需要 {MIN_PASSWORD_CHARS} 位字符"
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"密码过长（不超过 {MAX_PASSWORD_BYTES} 字节）"
    return None


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
        if key not in _login_failures and len(_login_failures) >= _LOGIN_FAIL_MAX_KEYS:
            _prune_all_expired(now)
        if key not in _login_failures and len(_login_failures) >= _LOGIN_FAIL_MAX_KEYS:
            # 键表仍然满：淘汰"最久没有失败记录"的键。绝不整体 clear()——否则攻击者
            # 只要把键表撑满就能把自己的失败计数一并清零，限流形同虚设。
            # 排序键 (是否已被封锁, 最后一次失败时间)：优先淘汰未达上限的键，
            # 保证已被封锁的账号不被洪水键挤出。
            victim = min(
                _login_failures,
                key=lambda k: (
                    len(_login_failures[k]) >= _LOGIN_FAIL_LIMIT,
                    _login_failures[k][-1] if _login_failures[k] else 0.0,
                ),
            )
            del _login_failures[victim]
        _prune_expired(key, now).append(now)


def _prune_all_expired(now: float) -> None:
    """清掉窗口内已无失败记录的键（键表的内存上限保护）"""
    for key in list(_login_failures.keys()):
        _prune_expired(key, now)
        if not _login_failures[key]:
            del _login_failures[key]


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
    credential_error = _validate_new_credentials(username, password)
    if credential_error:
        return JSONResponse(
            {"detail": credential_error},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

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
