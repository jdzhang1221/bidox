"""章节检索:把命中 chunk 的 section_id 扩到「章节 → 父章节 → 兄弟章节」,拼成方案体系。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.section import DocumentSection

logger = get_logger(__name__)


class SectionRetriever:
    """章节树扩上下文。"""

    def expand(
        self, section_ids: list[int | None], document_id: int | None = None
    ) -> list[dict[str, Any]]:
        """取回命中章节 + 父章节 + 兄弟章节,去重后按 (document_id, sort_order) 排序。"""
        ids = sorted({int(s) for s in section_ids if s is not None})
        if not ids:
            return []

        with session_scope() as session:
            hits = (
                session.execute(select(DocumentSection).where(DocumentSection.id.in_(ids)))
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
                        select(DocumentSection).where(DocumentSection.id.in_(parent_ids))
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
