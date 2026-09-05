"""混合检索:向量 + 关键词,RRF 融合 + 可选 Reranker。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.embedding.service import EmbeddingService
from app.retrieval.keyword import KeywordRetriever
from app.retrieval.reranker import Reranker
from app.retrieval.vector import VectorRetriever

logger = get_logger(__name__)


class HybridRetriever:
    """向量检索 + 关键词检索 -> RRF 融合 -> (Reranker) -> Top K。"""

    def __init__(self) -> None:
        self._vector = VectorRetriever()
        self._keyword = KeywordRetriever()
        self._embedding = EmbeddingService()
        self._reranker = Reranker() if settings.reranker_enabled else None

    def search(
        self,
        query: str,
        top_k: int | None = None,
        document_type: str | None = None,
        enterprise_id: int | None = None,
    ) -> list[dict[str, Any]]:
        top_k = top_k or settings.retrieval_top_k
        fusion_k = settings.retrieval_fusion_k

        # 1. 两路检索
        query_vector = self._embedding.embed_query(query)
        vector_results = self._vector.search(
            query_vector, top_k=fusion_k, document_type=document_type, enterprise_id=enterprise_id
        )
        keyword_results = self._keyword.search(
            query, top_k=fusion_k, document_type=document_type, enterprise_id=enterprise_id
        )

        # 2. RRF 融合
        fused = self._rrf(vector_results, keyword_results, k=60)

        # 3. 可选 reranker
        if self._reranker is not None:
            fused = self._reranker.rerank(query, fused, top_k=top_k)
        else:
            fused = fused[:top_k]
        return fused

    @staticmethod
    def _rrf(
        vec_results: list[dict[str, Any]],
        kw_results: list[dict[str, Any]],
        k: int = 60,
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion 融合。"""
        scores: dict[int, float] = {}
        doc_map: dict[int, dict[str, Any]] = {}

        for rank, item in enumerate(vec_results):
            cid = item["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            doc_map.setdefault(cid, item)
        for rank, item in enumerate(kw_results):
            cid = item["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            doc_map.setdefault(cid, item)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        out = []
        for cid, score in ranked:
            item = doc_map[cid]
            item["score"] = score
            out.append(item)
        return out
