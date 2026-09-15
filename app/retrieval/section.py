"""章节检索:把命中 chunk 的 section_id 扩到「章节 → 父章节 → 兄弟章节」,拼成方案体系。

`document_section` 表**没有** tenant_id 列，隔离靠「可信 document_id 集合」传递：
调用方只能把已经过 tenant/KB/生命周期过滤的 document_id 传进来，本节所有查询都被限制在该集合内。
集合为空时直接返回空，绝不放宽为全库扫描。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.tenant import require_allowed_document_ids
from app.models.section import DocumentSection

logger = get_logger(__name__)


class SectionRetriever:
    """章节树扩上下文。"""

    def expand(
        self,
        section_ids: list[int | None],
        allowed_document_ids: list[int] | set[int],
        document_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """取回命中章节 + 父章节 + 兄弟章节,去重后按 (document_id, sort_order) 排序。

        allowed_document_ids: 本次检索已授权的 document_id 集合(来自 tenant 过滤后的召回结果)。
        """
        ids = sorted({int(s) for s in section_ids if s is not None})
        if not ids:
            return []
        allowed = require_allowed_document_ids(allowed_document_ids, where="SectionRetriever.expand")
        if not allowed:
            return []

        with session_scope() as session:
            hits = (
                session.execute(
                    select(DocumentSection).where(
                        DocumentSection.id.in_(ids),
                        DocumentSection.document_id.in_(allowed),
                    )
                )
                .scalars()
                .all()
            )
            if document_id is not None:
                hits = [h for h in hits if h.document_id == document_id]

            # 父章节(命中章节的 parent_id 集合)
            parent_ids = {h.parent_id for h in hits if h.parent_id}
            parents: list[DocumentSection] = []
            if parent_ids:
                parents = (
                    session.execute(
                        select(DocumentSection).where(
                            DocumentSection.id.in_(parent_ids),
                            DocumentSection.document_id.in_(allowed),
                        )
                    )
                    .scalars()
                    .all()
                )

            # 兄弟章节(与命中章节同父,排除命中自身)
            siblings: list[DocumentSection] = []
            hit_parent_ids = {h.parent_id for h in hits if h.parent_id}
            if hit_parent_ids:
                stmt = select(DocumentSection).where(
                    DocumentSection.parent_id.in_(hit_parent_ids),
                    DocumentSection.id.not_in(ids),
                    DocumentSection.document_id.in_(allowed),
                )
                if document_id is not None:
                    stmt = stmt.where(DocumentSection.document_id == document_id)
                siblings = session.execute(stmt).scalars().all()

        merged: dict[int, DocumentSection] = {s.id: s for s in hits + parents + siblings}
        ordered = sorted(
            merged.values(), key=lambda s: (s.document_id or 0, s.sort_order or 0)
        )
        return [self._to_dict(s) for s in ordered]

    @staticmethod
    def _to_dict(s: DocumentSection) -> dict[str, Any]:
        return {
            "id": s.id,
            "document_id": s.document_id,
            "parent_id": s.parent_id,
            "section_no": s.section_no,
            "title": s.title,
            "level": s.level,
            "section_type": s.section_type,
            "content": s.content,
            "page_start": s.page_start,
            "page_end": s.page_end,
        }
