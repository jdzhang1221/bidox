"""文档解析 API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.api.schemas import ApiResponse, LocalParseRequest, ParseRequest
from app.core.storage import get_storage
from app.document.pipeline import DocumentPipeline, ParseResult
from app.knowledge.ingest import ingest_result
from app.tasks.schemas import ParseTaskMessage

router = APIRouter(prefix="/document", tags=["document"])


def _result_data(document_id: int | None, result: ParseResult) -> dict:
    """把 ParseResult 组装成统一响应 data。"""
    return {
        "document_id": document_id,
        "block_count": len(result.document.blocks),
        "section_count": len(result.flattened_sections),
        "chunk_count": len(result.chunks),
        "chunks": [c.model_dump() for c in result.chunks],
        "sections": [s.to_dict() for s in result.flattened_sections],
    }


def _ingest(
    result: ParseResult,
    document_id: int,
    req: ParseRequest | LocalParseRequest,
    *,
    file_name: str | None = None,
    storage_key: str | None = None,
) -> dict:
    """解析结果一次入库(document + 章节 + chunk + 可选 pattern),返回入库统计。"""
    return ingest_result(
        result,
        document_id=document_id,
        knowledge_base_id=req.knowledge_base_id,
        tenant_id=req.tenant_id,
        enterprise_id=req.enterprise_id,
        document_type=req.document_type,
        name=file_name,
        file_name=file_name,
        storage_key=storage_key,
        parser=result.document.meta.get("detected_type"),
        with_patterns=req.with_patterns,
    )


@router.post("/parse", response_model=ApiResponse)
def parse_document(req: ParseRequest) -> ApiResponse:
    """同步解析文档(开发调试用,生产走 MQ 异步);persist=True 时解析后向量化落库。"""
    message = ParseTaskMessage(
        task_id=f"sync-{req.document_id}",
        document_id=req.document_id,
        file_url=req.file_url,
        document_type=req.document_type,
    )
    storage = get_storage()
    local_path = storage.download(req.file_url)
    result = DocumentPipeline(document_id=req.document_id).run(
        local_path, document_type=req.document_type
    )
    data = _result_data(req.document_id, result)
    if req.persist:
        data["indexed"] = _ingest(
            result, req.document_id, req, file_name=Path(req.file_url).name, storage_key=req.file_url
        )
    return ApiResponse(data=data)


@router.post("/parse-local", response_model=ApiResponse)
def parse_local_document(req: LocalParseRequest) -> ApiResponse:
    """解析本地文件(本地调试用,直接传本地路径,不走存储下载);persist=True 时向量化落库。"""
    path = Path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"本地文件不存在: {req.path}")
    if req.persist and req.document_id is None:
        raise HTTPException(
            status_code=400, detail="persist=True 时必须提供 document_id(用于章节/chunk 落库)"
        )
    result = DocumentPipeline(document_id=req.document_id).run(
        path, document_type=req.document_type
    )
    data = _result_data(req.document_id, result)
    if req.persist:
        data["indexed"] = _ingest(result, req.document_id, req, file_name=path.name)
    return ApiResponse(data=data)
