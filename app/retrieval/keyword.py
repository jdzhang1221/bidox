"""关键词检索(PostgreSQL FTS)。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.chunk import DocumentChunk

logger = get_logger(__name__)


class KeywordRetriever:
    """基于 PostgreSQL 全文检索的关键词搜索。"""

    def search(
        self,
        query: str,
        top_k: int = 10,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """FTS 检索,返回含 rank 的结果。"""
        # 对中文,FTS 需配合 zhparser 等扩展;无 zhparser 时用 ILIKE 兜底。
        with session_scope() as session:
            # 兜底:ILIKE 关键词匹配(中文友好)
            like = f"%{query}%"
            stmt = select(DocumentChunk).where(DocumentChunk.content.ilike(like))
            if document_types:
                stmt = stmt.where(DocumentChunk.document_type.in_(document_types))
            if enterprise_id is not None:
                stmt = stmt.where(DocumentChunk.enterprise_id == enterprise_id)
            if knowledge_base_id is not None:
                stmt = stmt.where(DocumentChunk.knowledge_base_id == knowledge_base_id)
            stmt = stmt.limit(top_k)
            rows = session.execute(stmt).scalars().all()

        return [
            {
                "id": c.id,
                "content": c.content,
                "title": c.title,
                "section_type": c.section_type,
                "document_id": c.document_id,
                "section_id": c.section_id,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "score": 1.0,  # ILIKE 无相似度,统一给 1
                "score_type": "keyword",
            }
            for c in rows
        ]
