"""向量检索(pgvector)。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.chunk import DocumentChunk

logger = get_logger(__name__)


class VectorRetriever:
    """pgvector 余弦相似度检索。"""

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """返回 top_k 条 chunk,含相似度。"""
        with session_scope() as session:
            stmt = select(
                DocumentChunk,
                (DocumentChunk.embedding.cosine_distance(query_vector)).label("distance"),
            )
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
                "score": 1 - row.distance,  # 余弦距离 -> 相似度
                "score_type": "cosine",
            }
            for row in rows
        ]
