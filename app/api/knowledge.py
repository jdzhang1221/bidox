"""知识库 API。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ApiResponse, GenerateRequest, RagRequest, SearchRequest
from app.knowledge.service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/context", response_model=ApiResponse)
def build_context(req: SearchRequest) -> ApiResponse:
    """检索并拼装 evidence context(供标书生成使用)。"""
    service = KnowledgeService()
    context = service.build_context(req.query, top_k=req.top_k)
    return ApiResponse(data={"context": context})


@router.post("/generate", response_model=ApiResponse)
def generate(req: GenerateRequest) -> ApiResponse:
    """RAG 生成:检索 chunk → 拼 context → LLM 生成答案(一步到位)。

    返回 {query, answer, sources, context},sources 含溯源信息(rank/title/page/score)。
    """
    service = KnowledgeService()
    result = service.generate(
        req.query,
        top_k=req.top_k,
        document_type=req.document_type,
        enterprise_id=req.enterprise_id,
        knowledge_base_id=req.knowledge_base_id,
        system_prompt=req.system_prompt,
        pattern_top_k=req.pattern_top_k,
    )
    return ApiResponse(data=result)


@router.post("/rag", response_model=ApiResponse)
def rag(req: RagRequest) -> ApiResponse:
    """RAG V2 全链路:意图识别 → 双通道召回 → 章节扩上下文 → 去重 → Evidence Pack → LLM。

    返回 {query, intent, patterns, sections, evidences, enterprise_facts, answer, trace, context}。
    """
    service = KnowledgeService()
    result = service.rag(
        req.query,
        enterprise_id=req.enterprise_id,
        knowledge_base_id=req.knowledge_base_id,
        document_types=req.document_types or req.filters.document_types or None,
        pattern_top_k=req.retrieval.pattern_top_k,
        chunk_top_k=req.retrieval.chunk_top_k,
        pattern_types=req.filters.pattern_types or None,
        section_expand=req.retrieval.section_expand,
        deduplicate=req.retrieval.deduplicate,
        rerank=req.retrieval.rerank,
        system_prompt=req.system_prompt,
        requirements=req.requirements or None,
        score_items=req.score_items or None,
        tender_text=req.tender_text,
    )
    return ApiResponse(data=result)
