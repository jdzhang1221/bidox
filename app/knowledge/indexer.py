"""知识索引:把 Chunk 向量化后写入 pgvector。"""

from __future__ import annotations

from typing import Any

from app.core.database import session_scope
from app.core.logging import get_logger
from app.document.chunk.models import Chunk
from app.embedding.service import EmbeddingService
from app.models.chunk import DocumentChunk
from app.models.section import DocumentSection

logger = get_logger(__name__)


class KnowledgeIndexer:
    """将解析结果(chunks)索引到数据库。"""

    def __init__(self) -> None:
        self._embedding = EmbeddingService()

    def index_chunks(self, chunks: list[Chunk]) -> int:
        """向量化并批量写入 document_chunk。返回写入条数。"""
        if not chunks:
            return 0
        # 批量向量化
        texts = [c.content for c in chunks]
        vectors = self._embedding.embed_documents(texts)

        with session_scope() as session:
            for chunk, vector in zip(chunks, vectors):
                record = DocumentChunk(**chunk.to_db_dict(), embedding=vector)
                session.add(record)
        logger.info("已索引 %d 个 chunk", len(chunks))
        return len(chunks)

    def save_sections(self, sections: list[Any], document_id: int) -> dict[int, int]:
        """保存章节树,返回 {临时排序 -> 真实 section_id} 用于 chunk 回填。"""
        # sections 为展平后的 Section 列表
        id_map: dict[int, int] = {}
        with session_scope() as session:
            for i, sec in enumerate(sections):
                record = DocumentSection(
                    document_id=document_id,
                    parent_id=id_map.get(sec.parent_id) if sec.parent_id else None,
                    title=sec.title,
                    level=sec.level,
                    section_no=sec.section_no,
                    section_type=sec.section_type,
                    sort_order=i,
                    page_start=sec.page_start,
                    page_end=sec.page_end,
                    content=sec.content,
                )
                session.add(record)
                session.flush()
                id_map[i] = record.id
        return id_map
