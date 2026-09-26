from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))


@router.get("/knowledge")
async def knowledge_page(request: Request):
    username = request.session.get("username")
    if username is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    user_id = request.session.get("user_id")
    return templates.TemplateResponse(request, "func_main.html", {"username": username, "user_id": user_id})
