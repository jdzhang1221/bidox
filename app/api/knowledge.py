"""知识库 API。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ApiResponse, SearchRequest
from app.knowledge.service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/context", response_model=ApiResponse)
def build_context(req: SearchRequest) -> ApiResponse:
    """检索并拼装 evidence context(供标书生成使用)。"""
    service = KnowledgeService()
    context = service.build_context(req.query, top_k=req.top_k)
    return ApiResponse(data={"context": context})
