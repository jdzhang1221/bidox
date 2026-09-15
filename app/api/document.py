"""文档解析 API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.api.schemas import ApiResponse, DeleteIndexRequest, LocalParseRequest, ParseRequest
from app.core.storage import get_storage
from app.document.pipeline import DocumentPipeline, ParseResult
from app.knowledge.guard import IndexDecision, IndexRejected
from app.knowledge.ingest import delete_document_index, ingest_result
from app.tasks.schemas import ParseTaskMessage

router = APIRouter(prefix="/document", tags=["document"])

# 守卫判定 → HTTP 状态码：删除类用 410（Gone），版本/输入冲突用 409（Conflict）
_REJECT_STATUS = {
    IndexDecision.REJECT_DELETED: 410,
    IndexDecision.REJECT_QUARANTINED: 410,
    IndexDecision.REJECT_STALE: 409,
    IndexDecision.REJECT_VERSION_MISMATCH: 409,
    IndexDecision.REJECT_TENANT_MISMATCH: 409,
}


def _result_data(document_id: int | None, result: ParseResult) -> dict:
    """把 ParseResult 组装成统一响应 data（兼容 Java 侧字段名）。"""
    return {
        "document_id": document_id,
        "documentId": document_id,
        "parser": result.document.meta.get("detected_type"),
        "parser_version": None,
        "parserVersion": None,
        "block_count": len(result.document.blocks),
        "blockCount": len(result.document.blocks),
        "section_count": len(result.flattened_sections),
        "sectionCount": len(result.flattened_sections),
        "chunk_count": len(result.chunks),
        "chunkCount": len(result.chunks),
        "pattern_count": 0,
        "patternCount": 0,
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
    """解析结果一次入库(document + 章节 + chunk + 可选 pattern),返回入库统计。

    守卫拒绝（删除后晚到 / 旧版本 / 同版本不同输入）会抛 `IndexRejected`，
    这里映射成 409/410，让调用方（Java）能区分「重试无意义」与「可重试」。
    """
    try:
        return ingest_result(
            result,
            document_id=document_id,
            index_version=req.index_version,
            parse_log_id=getattr(req, "parse_log_id", None),
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
    except IndexRejected as exc:
        raise HTTPException(
            status_code=_REJECT_STATUS.get(exc.decision, 409),
            detail=f"{exc.decision.value}: {exc}",
        ) from exc


@router.post("/parse", response_model=ApiResponse)
def parse_document(req: ParseRequest) -> ApiResponse:
    """同步解析文档(开发调试用,生产走 MQ 异步);persist=True 时解析后向量化落库。"""
    if not req.storage_key and not req.file_url:
        raise HTTPException(status_code=400, detail="缺少 storageKey/fileUrl")

    storage_path = req.storage_key or req.file_url
    storage = get_storage(req.storage_provider)
    local_path = storage.download(storage_path)
    result = DocumentPipeline(document_id=req.document_id).run(
        local_path, document_type=req.document_type
    )
    data = _result_data(req.document_id, result)
    if req.persist:
        indexed = _ingest(
            result,
            req.document_id,
            req,
            file_name=req.file_name or Path(storage_path).name,
            storage_key=storage_path,
        )
        data["pattern_count"] = indexed.get("pattern_count", 0)
        data["patternCount"] = indexed.get("pattern_count", 0)
        data["indexed"] = indexed
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


@router.post("/delete", response_model=ApiResponse)
def delete_document(req: DeleteIndexRequest) -> ApiResponse:
    """幂等删除文档索引（写墓碑 + 清理 section/chunk/pattern_source）。

    幂等：重复调用返回 `already_deleted=True`，不报错 —— Java 侧的重试因此可以无脑重发。
    删除后 guard 墓碑保留，任何晚到的解析任务都会被 410 拒绝，索引不会复活。
    """
    data = delete_document_index(req.document_id, tenant_id=req.tenant_id)
    return ApiResponse(data=data)

