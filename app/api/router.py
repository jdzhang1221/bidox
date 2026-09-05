"""汇总所有 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import document, knowledge, retrieval, tender

api_router = APIRouter()
api_router.include_router(document.router)
api_router.include_router(knowledge.router)
api_router.include_router(retrieval.router)
api_router.include_router(tender.router)
