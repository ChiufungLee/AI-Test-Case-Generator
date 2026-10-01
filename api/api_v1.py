from fastapi import APIRouter

from api.endpoints import knowledge_pages

api_router = APIRouter()
api_router.include_router(knowledge_pages.router, tags=["知识库"])
