"""检索 API。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ApiResponse, SearchRequest
from app.knowledge.service import KnowledgeService

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.post("/search", response_model=ApiResponse)
def search(req: SearchRequest) -> ApiResponse:
    """混合检索。"""
    service = KnowledgeService()
    results = service.search(
        req.query,
        top_k=req.top_k,
        document_type=req.document_type,
        enterprise_id=req.enterprise_id,
    )
    return ApiResponse(data={"results": results, "count": len(results)})
