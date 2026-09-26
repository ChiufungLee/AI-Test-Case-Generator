from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from models.database import get_db
from services.auth_service import AuthService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html")


@router.post("/register")
async def register_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    register_result = await AuthService.create_user(db, username, password)
    if not register_result["success"]:
        return JSONResponse(
            {"detail": register_result["error"]},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.get("/", response_class=HTMLResponse)
async def main_page(request: Request):
    username = request.session.get("username")
    if username is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "用户会话已失效，请重新登录"},
        )
    return RedirectResponse(url="/chat", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/login")
async def login_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    auth_result = await AuthService.login_user(db, username, password)
    if not auth_result["success"]:
        return JSONResponse(
            {"detail": "用户名或密码错误"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    request.session["user_id"] = auth_result["user_id"]
    request.session["username"] = auth_result["username"]
    request.session["login_time"] = auth_result["login_time"]

    response = RedirectResponse(url="/chat", status_code=status.HTTP_303_SEE_OTHER)
    return response


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login?logout=true", status_code=status.HTTP_303_SEE_OTHER)
