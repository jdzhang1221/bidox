"""Embedding 服务:批量去重 + 分批。"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.embedding.base import BaseEmbedder
from app.embedding.bge_m3 import BgeM3Embedder

logger = get_logger(__name__)


class EmbeddingService:
    """批量向量化服务。"""

    def __init__(self, embedder: BaseEmbedder | None = None) -> None:
        self._embedder = embedder or BgeM3Embedder()

    @property
    def embedder(self) -> BaseEmbedder:
        return self._embedder

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """文档批量向量化(去重 + 分批)。"""
        if not texts:
            return []
        # 去重但保持顺序
        unique, index_map = self._dedupe(texts)
        vectors = self._batch_embed(unique)
        return [vectors[index_map[i]] for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        return self._embedder.embed_query(text)

    # --- 内部 ---
    @staticmethod
    def _dedupe(texts: list[str]) -> tuple[list[str], dict[int, int]]:
        """去重,返回 (唯一文本列表, 原索引->唯一索引映射)。"""
        seen: dict[str, int] = {}
        unique: list[str] = []
        index_map: dict[int, int] = {}
        for i, t in enumerate(texts):
            key = t.strip()
            if key in seen:
                index_map[i] = seen[key]
            else:
                seen[key] = len(unique)
                unique.append(key)
                index_map[i] = seen[key]
        return unique, index_map

    def _batch_embed(self, texts: list[str]) -> list[list[float]]:
        batch_size = settings.embedding_batch_size
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            vectors.extend(self._embedder.embed(batch))
        return vectors
