"""文档解析 API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from app.api.schemas import ApiResponse, LocalParseRequest, ParseRequest
from app.core.storage import get_storage
from app.document.pipeline import DocumentPipeline, ParseResult
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


@router.post("/parse", response_model=ApiResponse)
def parse_document(req: ParseRequest) -> ApiResponse:
    """同步解析文档(开发调试用,生产走 MQ 异步)。"""
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
    return ApiResponse(data=_result_data(req.document_id, result))


@router.post("/parse-local", response_model=ApiResponse)
def parse_local_document(req: LocalParseRequest) -> ApiResponse:
    """解析本地文件(本地调试用,直接传本地路径,不走存储下载)。"""
    path = Path(req.path)
    if not path.exists():
        raise FileNotFoundError(f"本地文件不存在: {req.path}")
    result = DocumentPipeline(document_id=req.document_id).run(
        path, document_type=req.document_type
    )
    return ApiResponse(data=_result_data(req.document_id, result))
