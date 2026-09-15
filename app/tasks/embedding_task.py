"""向量化任务处理:把 chunk 向量化写入 pgvector。

本路径**只补向量、不改结构**，因此守卫用 `ignore_input_hash=True`：
同版本重跑本就是空操作，不需要再拿输入摘要比对（摘要口径与完整 ingest 不同，比了只会误拒）。
生命周期（DELETED / QUARANTINED）与旧版本（STALE）仍然严格拒绝。
"""

from __future__ import annotations

from app.core.database import session_scope
from app.core.logging import get_logger
from app.document.chunk.models import Chunk
from app.knowledge.guard import (
    IndexDecision,
    IndexRejected,
    begin_index,
    commit_index,
    compute_index_input_hash,
)
from app.knowledge.indexer import KnowledgeIndexer
from app.knowledge.ingest import resolve_index_version
from app.tasks.schemas import EmbeddingTaskMessage

logger = get_logger(__name__)


def handle_embedding_task(message: EmbeddingTaskMessage, chunks: list[Chunk] | None = None) -> int:
    """处理向量化任务。

    chunks 通常来自解析阶段暂存;若为 None,则由调用方从缓存/DB 取。
    """
    logger.info("处理向量化任务: task=%s document=%s", message.task_id, message.document_id)
    if not chunks:
        logger.warning("无 chunk 可向量化,跳过")
        return 0

    version = resolve_index_version(index_version=message.index_version, parse_log_id=None)
    digest = compute_index_input_hash(
        file_hash=None,
        document_type=None,
        knowledge_base_id=message.knowledge_base_id,
        parser=None,
        extra={"source": "embedding_task"},
    )

    # 阶段 1：短事务校验（生命周期 + 版本）
    with session_scope() as session:
        _, decision = begin_index(
            session,
            tenant_id=message.tenant_id,
            document_id=message.document_id,
            index_version=version,
            input_hash=digest,
            ignore_input_hash=True,
        )
    if decision.rejected:
        raise IndexRejected(
            decision,
            f"文档 {message.document_id} 版本 {version} 的向量化被拒绝：{decision.value}",
        )
    if decision is IndexDecision.IDEMPOTENT:
        logger.info("文档 %s 版本 %s 已提交，跳过向量化", message.document_id, version)
        return 0

    # 阶段 2：事务外向量化（慢操作，不占 guard 行锁）
    indexer = KnowledgeIndexer()
    vectors = indexer.embed_chunks(chunks)

    # 阶段 3：短事务重新校验 + 写入 + 提交版本
    with session_scope() as session:
        _, decision = begin_index(
            session,
            tenant_id=message.tenant_id,
            document_id=message.document_id,
            index_version=version,
            input_hash=digest,
            require_existing=True,
            ignore_input_hash=True,
        )
        if decision.rejected:
            raise IndexRejected(
                decision,
                f"文档 {message.document_id} 版本 {version} 的向量化被拒绝：{decision.value}",
            )
        if decision is IndexDecision.IDEMPOTENT:
            return 0
        count = indexer.index_chunks(
            chunks,
            tenant_id=message.tenant_id,
            knowledge_base_id=message.knowledge_base_id,
            enterprise_id=message.enterprise_id,
            index_version=version,
            vectors=vectors,
            session=session,
        )
        commit_index(
            session,
            document_id=message.document_id,
            index_version=version,
            input_hash=digest,
        )
    return count
