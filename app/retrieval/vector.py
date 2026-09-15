"""向量检索(pgvector)。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.tenant import require_tenant_id
from app.models.chunk import DocumentChunk
from app.models.index_guard import active_document_clause

logger = get_logger(__name__)


class VectorRetriever:
    """pgvector 余弦相似度检索。"""

    def search(
        self,
        query_vector: list[float],
        tenant_id: int,
        top_k: int = 10,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """返回 top_k 条 chunk,含相似度。"""
        tenant_id = require_tenant_id(tenant_id, where="VectorRetriever.search")
        with session_scope() as session:
            stmt = select(
                DocumentChunk,
                (DocumentChunk.embedding.cosine_distance(query_vector)).label("distance"),
            )
            # 租户是硬边界:不选知识库只是不加 knowledge_base_id 条件,租户条件永远存在。
            stmt = stmt.where(DocumentChunk.tenant_id == tenant_id)
            # 生命周期也是硬边界:DELETED / QUARANTINED 的文档不得被召回。
            stmt = stmt.where(active_document_clause(DocumentChunk.document_id))
            if document_types:
                stmt = stmt.where(DocumentChunk.document_type.in_(document_types))
            if enterprise_id is not None:
                stmt = stmt.where(DocumentChunk.enterprise_id == enterprise_id)
            if knowledge_base_id is not None:
                stmt = stmt.where(DocumentChunk.knowledge_base_id == knowledge_base_id)
            stmt = (
                stmt.where(DocumentChunk.embedding.is_not(None))
                .order_by(text("distance"))
                .limit(top_k)
            )
            rows = session.execute(stmt).all()
        return [
            {
                "id": row.DocumentChunk.id,
                "content": row.DocumentChunk.content,
                "title": row.DocumentChunk.title,
                "section_type": row.DocumentChunk.section_type,
                "document_id": row.DocumentChunk.document_id,
                "section_id": row.DocumentChunk.section_id,
                "page_start": row.DocumentChunk.page_start,
                "page_end": row.DocumentChunk.page_end,
                "index_version": row.DocumentChunk.index_version,
                # similarity 是向量召回的原始余弦相似度，后续 RRF/reranker 不得覆盖。
                "similarity": 1 - row.distance,
                "retrieval_score": 1 - row.distance,
                # score 保留为兼容字段；新契约使用 retrieval_score + score_type。
                "score": 1 - row.distance,
                "score_type": "cosine",
            }
            for row in rows
        ]
