"""混合检索:向量 + 关键词,RRF 融合 + 可选 Reranker;另含方案组件(Pattern)检索通道。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.embedding.service import EmbeddingService
from app.retrieval.keyword import KeywordRetriever
from app.retrieval.pattern import PatternRetriever
from app.retrieval.query import QueryUnderstanding
from app.retrieval.reranker import Reranker
from app.retrieval.vector import VectorRetriever

logger = get_logger(__name__)

# 方案级召回默认条数(设计: Pattern Top5)
DEFAULT_PATTERN_TOP_K = 5


class HybridRetriever:
    """双通道检索:方案组件(Pattern) + 历史证据(Chunk)。

    - search() / search_patterns() 分别暴露证据通道与方案通道。
    - retrieve() 一次返回两路结果,供 Evidence Pack 组装。
    """

    def __init__(self) -> None:
        self._vector = VectorRetriever()
        self._keyword = KeywordRetriever()
        self._pattern = PatternRetriever()
        self._embedding = EmbeddingService()
        self._reranker = Reranker() if settings.reranker_enabled else None

    # --- 证据通道(chunk) ---
    def search(
        self,
        query: str,
        top_k: int | None = None,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        top_k = top_k or settings.retrieval_top_k
        fusion_k = settings.retrieval_fusion_k

        # 1. 两路检索
        query_vector = self._embedding.embed_query(query)
        vector_results = self._vector.search(
            query_vector,
            top_k=fusion_k,
            document_types=document_types,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
        )
        keyword_results = self._keyword.search(
            query,
            top_k=fusion_k,
            document_types=document_types,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
        )

        # 2. RRF 融合
        fused = self._rrf(vector_results, keyword_results, k=60)

        # 3. 可选 reranker(env 启用 + 本次请求未禁用时才重排)
        if self._reranker is not None and rerank:
            fused = self._reranker.rerank(query, fused, top_k=top_k)
        else:
            fused = fused[:top_k]
        return fused

    # --- 方案通道(pattern) ---
    def search_patterns(
        self,
        query: str,
        top_k: int | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        pattern_types: list[str] | None = None,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        top_k = top_k or DEFAULT_PATTERN_TOP_K
        query_vector = self._embedding.embed_query(query)
        # recall-then-rerank:启用 reranker 时先多召回,再重排到 top_k
        recall_k = settings.reranker_recall_k if (self._reranker is not None and rerank) else top_k
        patterns = self._pattern.search(
            query_vector,
            top_k=recall_k,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            pattern_types=pattern_types,
        )
        # 类型过滤后为空 → 回退为不过滤,保召回不丢
        if not patterns and pattern_types:
            logger.info("pattern 类型过滤后为空,回退为不过滤: %s", pattern_types)
            patterns = self._pattern.search(
                query_vector,
                top_k=recall_k,
                enterprise_id=enterprise_id,
                knowledge_base_id=knowledge_base_id,
            )
        # 只要候选 > 1 就重排:类型强过滤后常只剩 2~3 条(<=top_k),
        # 若仍用 len>top_k 作门槛,reranker 将永不介入,SP021 这类误标类型噪声
        # 只能按原始 cosine 排,失去"recall-then-rerank"的语义重排能力。
        if self._reranker is not None and rerank and len(patterns) > 1:
            return self._reranker.rerank(query, patterns, top_k=top_k)
        return patterns[:top_k]

    # --- 双通道 ---
    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        pattern_top_k: int | None = None,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        pattern_types: list[str] | None = None,
        rerank: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """双通道召回,返回 {"patterns": [...], "evidences": [...]}。

        未显式传入 pattern_types 时,先做意图识别以过滤 pattern 类型(解决串题)。
        """
        if pattern_types is None:
            intent = QueryUnderstanding().classify(query)
            pattern_types = intent.pattern_types or None
        patterns = self.search_patterns(
            query,
            top_k=pattern_top_k,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            pattern_types=pattern_types,
            rerank=rerank,
        )
        evidences = self.search(
            query,
            top_k=top_k,
            document_types=document_types,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            rerank=rerank,
        )
        return {"patterns": patterns, "evidences": evidences}

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
            item["score_type"] = "rrf"
            out.append(item)
        return out
