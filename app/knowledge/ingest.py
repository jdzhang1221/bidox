"""文档入库编排:document 记录 + 章节 + chunk + pattern,全链路关联。"""

from __future__ import annotations

from typing import Any

from app.core.database import session_scope
from app.core.logging import get_logger
from app.knowledge.indexer import KnowledgeIndexer
from app.models.document import DocumentRecord

logger = get_logger(__name__)


def _ensure_document(
    document_id: int,
    *,
    knowledge_base_id: int | None = None,
    tenant_id: int | None = None,
    enterprise_id: int | None = None,
    name: str | None = None,
    file_name: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    document_type: str | None = None,
    storage_key: str | None = None,
    file_hash: str | None = None,
    parser: str | None = None,
) -> None:
    """按 document_id 建/更新 document 记录(幂等 upsert)。"""
    with session_scope() as session:
        record = session.get(DocumentRecord, document_id)
        if record is None:
            record = DocumentRecord(
                id=document_id,
                tenant_id=tenant_id,
                enterprise_id=enterprise_id,
                knowledge_base_id=knowledge_base_id,
                name=name,
                file_name=file_name or f"doc-{document_id}",
                file_type=file_type or "",
                file_size=file_size,
                storage_key=storage_key,
                document_type=document_type or "historical_bid",
                status="embedded",
                parser=parser,
                file_hash=file_hash,
            )
            session.add(record)
        else:
            if knowledge_base_id is not None:
                record.knowledge_base_id = knowledge_base_id
            if name:
                record.name = name
            record.status = "embedded"
    logger.info("document 记录就绪: id=%s", document_id)


def ingest_result(
    result: Any,
    *,
    document_id: int,
    knowledge_base_id: int | None = None,
    tenant_id: int | None = None,
    enterprise_id: int | None = None,
    document_type: str = "historical_bid",
    name: str | None = None,
    file_name: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    storage_key: str | None = None,
    file_hash: str | None = None,
    parser: str | None = None,
    with_patterns: bool = True,
) -> dict[str, Any]:
    """一次入库:document 记录 + 章节 + chunk,可选同步抽取并入库方案组件。

    返回 {"document_id", "section_count", "chunk_count", "pattern_count"}。
    """
    _ensure_document(
        document_id,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id,
        enterprise_id=enterprise_id,
        name=name,
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        document_type=document_type,
        storage_key=storage_key,
        file_hash=file_hash,
        parser=parser,
    )

    indexer = KnowledgeIndexer()
    stats = indexer.index_document(
        result,
        document_id,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id,
        enterprise_id=enterprise_id,
    )

    pattern_count = 0
    if with_patterns:
        from app.pattern.indexer import SolutionPatternIndexer

        pattern_indexer = SolutionPatternIndexer()
        pairs = pattern_indexer.extract(result, doc_meta={"title": name or file_name})
        pattern_count = pattern_indexer.save(
            pairs,
            id_map=stats["id_map"],
            document_id=document_id,
            knowledge_base_id=knowledge_base_id,
            tenant_id=tenant_id,
            enterprise_id=enterprise_id,
        )

    return {
        "document_id": document_id,
        "section_count": stats["section_count"],
        "chunk_count": stats["chunk_count"],
        "pattern_count": pattern_count,
    }
