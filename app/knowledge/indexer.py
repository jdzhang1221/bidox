"""知识索引:把 Chunk 向量化后写入 pgvector。"""

from __future__ import annotations

from typing import Any

from app.core.database import session_scope
from app.core.logging import get_logger
from app.document.chunk.models import Chunk
from app.document.section.models import flatten_sections_with_parent
from app.embedding.service import EmbeddingService
from app.models.chunk import DocumentChunk
from app.models.section import DocumentSection

logger = get_logger(__name__)


class KnowledgeIndexer:
    """将解析结果(chunks)索引到数据库。"""

    def __init__(self) -> None:
        self._embedding = EmbeddingService()

    def index_chunks(
        self,
        chunks: list[Chunk],
        *,
        knowledge_base_id: int | None = None,
        tenant_id: int | None = None,
        enterprise_id: int | None = None,
    ) -> int:
        """向量化并批量写入 document_chunk。返回写入条数。"""
        if not chunks:
            return 0
        # 批量向量化
        texts = [c.content for c in chunks]
        vectors = self._embedding.embed_documents(texts)

        with session_scope() as session:
            for chunk, vector in zip(chunks, vectors):
                record = DocumentChunk(**chunk.to_db_dict(), embedding=vector)
                # chunk 归属冗余,便于按库/租户过滤,与 section/document 保持一致
                if knowledge_base_id is not None:
                    record.knowledge_base_id = knowledge_base_id
                if tenant_id is not None:
                    record.tenant_id = tenant_id
                if enterprise_id is not None:
                    record.enterprise_id = enterprise_id
                session.add(record)
        logger.info("已索引 %d 个 chunk", len(chunks))
        return len(chunks)

    def save_sections(
        self,
        sections: list[Any],
        document_id: int,
        *,
        knowledge_base_id: int | None = None,
    ) -> dict[int, int]:
        """保存章节树,返回 {临时 id -> 真实 section_id} 用于 chunk 回填。"""
        # sections 为展平后的 Section 列表(sec.id = 临时 id, sec.parent_id = 临时父 id)
        id_map: dict[int, int] = {}
        with session_scope() as session:
            for i, sec in enumerate(sections):
                record = DocumentSection(
                    document_id=document_id,
                    knowledge_base_id=knowledge_base_id,
                    # 注意用 is not None:临时 id 可能为 0(首个顶层章节),不能当 falsy 处理
                    parent_id=id_map.get(sec.parent_id) if sec.parent_id is not None else None,
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
                id_map[sec.id] = record.id
        return id_map

    def index_document(
        self,
        result: Any,
        document_id: int,
        *,
        knowledge_base_id: int | None = None,
        tenant_id: int | None = None,
        enterprise_id: int | None = None,
    ) -> dict[str, Any]:
        """把一份解析结果(ParseResult)完整入库:建表/pgvector 扩展 → 章节树 → 向量化 chunk。

        返回 {"section_count": int, "chunk_count": int, "id_map": {临时 id -> 真实 section_id}}。
        """
        from app.core.database import add_vector_extension, init_db

        # 幂等:表/扩展不存在时才创建,已存在则跳过。开发期省去手动迁移。
        add_vector_extension()
        init_db()

        sections = flatten_sections_with_parent(result.sections)
        id_map = self.save_sections(sections, document_id, knowledge_base_id=knowledge_base_id)

        # 回填 chunk.section_id:临时 id -> 真实主键
        for chunk in result.chunks:
            if chunk.section_id is not None:
                chunk.section_id = id_map.get(chunk.section_id)

        chunk_count = self.index_chunks(
            result.chunks,
            knowledge_base_id=knowledge_base_id,
            tenant_id=tenant_id,
            enterprise_id=enterprise_id,
        )
        return {"section_count": len(sections), "chunk_count": chunk_count, "id_map": id_map}
