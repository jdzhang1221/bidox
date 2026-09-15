"""知识库 API。"""

from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.schemas import ApiResponse, GenerateRequest, QaStreamRequest, RagRequest, SearchRequest
from app.knowledge.qa_stream import prepare_qa, stream_qa
from app.knowledge.service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/qa-stream")
async def qa_stream(req: QaStreamRequest) -> StreamingResponse:
    """企业知识问答真流式接口（Java 内部调用）。

    同步 DB/Embedding/Reranker 在有界 worker 池内完成，且在返回 StreamingResponse 前完成，
    因而 tenant/检索/pre-stream 错误仍可由 FastAPI 正常返回 JSON；首个事件后只使用 SSE 终态。
    """
    deadline = time.monotonic() + req.timeout_millis / 1000.0
    history = [item.model_dump() for item in req.history]
    prepared = await prepare_qa(
        question=req.question,
        history=history,
        tenant_id=req.tenant_id,
        knowledge_base_id=req.knowledge_base_id,
        top_k=req.top_k,
        rerank=req.rerank,
        deadline=deadline,
    )
    return StreamingResponse(
        stream_qa(prepared, deadline=deadline),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/context", response_model=ApiResponse)
def build_context(req: SearchRequest) -> ApiResponse:
    """检索并拼装 evidence context(供标书生成使用)。"""
    service = KnowledgeService()
    context = service.build_context(req.query, tenant_id=req.tenant_id, top_k=req.top_k)
    return ApiResponse(data={"context": context})


@router.post("/generate", response_model=ApiResponse)
def generate(req: GenerateRequest) -> ApiResponse:
    """RAG 生成:检索 chunk → 拼 context → LLM 生成答案(一步到位)。

    返回 {query, answer, sources, context},sources 含溯源信息(rank/title/page/score)。
    """
    service = KnowledgeService()
    result = service.generate(
        req.query,
        tenant_id=req.tenant_id,
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
        tenant_id=req.tenant_id,
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
