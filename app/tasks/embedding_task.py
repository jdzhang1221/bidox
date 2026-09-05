"""向量化任务处理:把 chunk 向量化写入 pgvector。"""

from __future__ import annotations

from app.core.logging import get_logger
from app.document.chunk.models import Chunk
from app.knowledge.indexer import KnowledgeIndexer
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
    indexer = KnowledgeIndexer()
    return indexer.index_chunks(chunks)
