"""文档解析 API。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ApiResponse, ParseRequest
from app.core.storage import get_storage
from app.document.pipeline import DocumentPipeline
from app.tasks.schemas import ParseTaskMessage

router = APIRouter(prefix="/document", tags=["document"])


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
    return ApiResponse(
        data={
            "document_id": req.document_id,
            "block_count": len(result.document.blocks),
            "section_count": len(result.flattened_sections),
            "chunk_count": len(result.chunks),
            "chunks": [c.model_dump() for c in result.chunks],
            "sections": [s.to_dict() for s in result.flattened_sections],
        }
    )
