"""知识索引:把 Chunk 向量化后写入 pgvector。"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.tenant import require_tenant_id
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

    def embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        """只做向量化，**不碰数据库**。

        向量化是慢操作（本地模型 / 远端 embedding 服务），必须放在守卫行锁之外执行，
        否则一次 ingest 会长时间占住 guard 行，拖住并发删除与更高版本的替换。
        """
        if not chunks:
            return []
        return self._embedding.embed_documents([c.content for c in chunks])

    def index_chunks(
        self,
        chunks: list[Chunk],
        *,
        tenant_id: int,
        knowledge_base_id: int | None = None,
        enterprise_id: int | None = None,
        index_version: int | None = None,
        vectors: list[list[float]] | None = None,
        session: Session | None = None,
    ) -> int:
        """批量写入 document_chunk。返回写入条数。

        vectors 为 None 时在本方法内向量化（向后兼容直接调用方）；
        由 ingest 编排时请传入 `embed_chunks()` 的预计算结果。
        """
        if not chunks:
            return 0
        tenant_id = require_tenant_id(tenant_id, where="KnowledgeIndexer.index_chunks")
        if vectors is None:
            vectors = self.embed_chunks(chunks)
        if len(vectors) != len(chunks):
            raise ValueError(
                f"index_chunks: 向量数({len(vectors)})与 chunk 数({len(chunks)})不一致"
            )

        context = nullcontext(session) if session is not None else session_scope()
        with context as db_session:
            for chunk, vector in zip(chunks, vectors):
                record = DocumentChunk(**chunk.to_db_dict(), embedding=vector)
                # chunk 归属冗余,便于按库/租户过滤,与 section/document 保持一致
                record.tenant_id = tenant_id
                record.index_version = index_version
                if knowledge_base_id is not None:
                    record.knowledge_base_id = knowledge_base_id
                if enterprise_id is not None:
                    record.enterprise_id = enterprise_id
                db_session.add(record)
        logger.info("已索引 %d 个 chunk（index_version=%s）", len(chunks), index_version)
        return len(chunks)

    def save_sections(
        self,
        sections: list[Any],
        document_id: int,
        *,
        knowledge_base_id: int | None = None,
        index_version: int | None = None,
        session: Session | None = None,
    ) -> dict[int, int]:
        """保存章节树,返回 {临时 id -> 真实 section_id} 用于 chunk 回填。"""
        # sections 为展平后的 Section 列表(sec.id = 临时 id, sec.parent_id = 临时父 id)
        id_map: dict[int, int] = {}
        context = nullcontext(session) if session is not None else session_scope()
        with context as db_session:
            for i, sec in enumerate(sections):
                record = DocumentSection(
                    document_id=document_id,
                    knowledge_base_id=knowledge_base_id,
                    index_version=index_version,
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
                db_session.add(record)
                db_session.flush()
                id_map[sec.id] = record.id
        return id_map

    def index_document(
        self,
        result: Any,
        document_id: int,
        *,
        tenant_id: int,
        knowledge_base_id: int | None = None,
        enterprise_id: int | None = None,
        index_version: int | None = None,
        vectors: list[list[float]] | None = None,
        session: Session | None = None,
        prepare_schema: bool = True,
    ) -> dict[str, Any]:
        """把一份解析结果(ParseResult)完整入库:建表/pgvector 扩展 → 章节树 → 向量化 chunk。

        返回 {"section_count": int, "chunk_count": int, "id_map": {临时 id -> 真实 section_id}}。
        """
        from app.core.database import add_vector_extension, init_db

        tenant_id = require_tenant_id(tenant_id, where="KnowledgeIndexer.index_document")
        # 幂等:表/扩展不存在时才创建,已存在则跳过。开发期省去手动迁移。
        if prepare_schema:
            add_vector_extension()
            init_db()

        sections = flatten_sections_with_parent(result.sections)
        id_map = self.save_sections(
            sections,
            document_id,
            knowledge_base_id=knowledge_base_id,
            index_version=index_version,
            session=session,
        )

        # 回填 chunk.section_id:临时 id -> 真实主键
        for chunk in result.chunks:
            if chunk.section_id is not None:
                chunk.section_id = id_map.get(chunk.section_id)

        chunk_count = self.index_chunks(
            result.chunks,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            enterprise_id=enterprise_id,
            index_version=index_version,
            vectors=vectors,
            session=session,
        )
        return {"section_count": len(sections), "chunk_count": chunk_count, "id_map": id_map}
