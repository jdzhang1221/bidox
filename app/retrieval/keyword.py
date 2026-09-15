"""关键词检索(PostgreSQL FTS)。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.tenant import require_tenant_id
from app.models.chunk import DocumentChunk
from app.models.index_guard import active_document_clause

logger = get_logger(__name__)


class KeywordRetriever:
    """基于 PostgreSQL 全文检索的关键词搜索。"""

    def search(
        self,
        query: str,
        tenant_id: int,
        top_k: int = 10,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """FTS 检索,返回含 rank 的结果。"""
        tenant_id = require_tenant_id(tenant_id, where="KeywordRetriever.search")
        # 对中文,FTS 需配合 zhparser 等扩展;无 zhparser 时用 ILIKE 兜底。
        # 注意:ILIKE 只是降级策略,只能放宽匹配方式,不能放宽 tenant/KB/文档类型过滤。
        with session_scope() as session:
            like = f"%{query}%"
            stmt = select(DocumentChunk).where(DocumentChunk.content.ilike(like))
            stmt = stmt.where(DocumentChunk.tenant_id == tenant_id)
            # 生命周期硬边界:DELETED / QUARANTINED 的文档不得被召回。
            stmt = stmt.where(active_document_clause(DocumentChunk.document_id))
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
                "index_version": c.index_version,
                # keyword-only 没有向量相似度，必须保持 null，不能伪装成 1.0。
                "similarity": None,
                "retrieval_score": 1.0,
                "score": 1.0,  # 兼容旧调用；新契约读取 retrieval_score
                "score_type": "keyword",
            }
            for c in rows
        ]
