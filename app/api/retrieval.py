"""检索 API。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ApiResponse, PatternSearchRequest, SearchRequest
from app.knowledge.service import KnowledgeService

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.post("/search", response_model=ApiResponse)
def search(req: SearchRequest) -> ApiResponse:
    """混合检索(历史证据 chunk)。"""
    service = KnowledgeService()
    document_types = req.document_types or ([req.document_type] if req.document_type else None)
    results = service.search(
        req.query,
        top_k=req.top_k,
        document_types=document_types,
        enterprise_id=req.enterprise_id,
        knowledge_base_id=req.knowledge_base_id,
    )
    return ApiResponse(data={"results": results, "count": len(results)})


@router.post("/patterns", response_model=ApiResponse)
def search_patterns(req: PatternSearchRequest) -> ApiResponse:
    """方案组件检索(含 source 溯源)。"""
    service = KnowledgeService()
    patterns = service.search_patterns(
        req.query,
        top_k=req.top_k,
        enterprise_id=req.enterprise_id,
        knowledge_base_id=req.knowledge_base_id,
        pattern_types=req.pattern_types,
    )
    return ApiResponse(data={"patterns": patterns, "count": len(patterns)})
