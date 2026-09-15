"""混合检索:向量 + 关键词,RRF 融合 + 可选 Reranker;另含方案组件(Pattern)检索通道。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.tenant import require_tenant_id
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
    - 所有入口都要求 tenant_id;type 过滤回退只能放宽类型,不能放宽租户。
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
        tenant_id: int,
        top_k: int | None = None,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        tenant_id = require_tenant_id(tenant_id, where="HybridRetriever.search")
        top_k = top_k or settings.retrieval_top_k
        fusion_k = settings.retrieval_fusion_k

        # 1. 两路检索
        query_vector = self._embedding.embed_query(query)
        vector_results = self._vector.search(
            query_vector,
            tenant_id=tenant_id,
            top_k=fusion_k,
            document_types=document_types,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
        )
        keyword_results = self._keyword.search(
            query,
            tenant_id=tenant_id,
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
        tenant_id: int,
        top_k: int | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        pattern_types: list[str] | None = None,
        rerank: bool = True,
        pattern_document_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """方案组件召回。

        `pattern_document_types` 是**来源文档类型白名单**（§4.4）：默认 QA 白名单（排除 tender），
        显式传入也只会在白名单内取交集，tender 永远进不来。
        注意它与证据通道的 `document_types` 是**两个独立的过滤维度**：
        证据类型过滤不应静默改变方案组件的召回范围。
        """
        tenant_id = require_tenant_id(tenant_id, where="HybridRetriever.search_patterns")
        top_k = top_k or DEFAULT_PATTERN_TOP_K
        query_vector = self._embedding.embed_query(query)
        # recall-then-rerank:启用 reranker 时先多召回,再重排到 top_k
        recall_k = settings.reranker_recall_k if (self._reranker is not None and rerank) else top_k
        patterns = self._pattern.search(
            query_vector,
            tenant_id=tenant_id,
            top_k=recall_k,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            pattern_types=pattern_types,
            document_types=pattern_document_types,
        )
        # 类型过滤后为空 → 回退为不过滤类型,保召回不丢(租户/KB/来源类型过滤仍然保留)
        if not patterns and pattern_types:
            logger.info("pattern 类型过滤后为空,回退为不过滤: %s", pattern_types)
            patterns = self._pattern.search(
                query_vector,
                tenant_id=tenant_id,
                top_k=recall_k,
                enterprise_id=enterprise_id,
                knowledge_base_id=knowledge_base_id,
                document_types=pattern_document_types,
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
        tenant_id: int,
        top_k: int | None = None,
        pattern_top_k: int | None = None,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        pattern_types: list[str] | None = None,
        rerank: bool = True,
        pattern_document_types: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """双通道召回,返回 {"patterns": [...], "evidences": [...]}。

        未显式传入 pattern_types 时,先做意图识别以过滤 pattern 类型(解决串题)。
        """
        tenant_id = require_tenant_id(tenant_id, where="HybridRetriever.retrieve")
        if pattern_types is None:
            intent = QueryUnderstanding().classify(query)
            pattern_types = intent.pattern_types or None
        patterns = self.search_patterns(
            query,
            tenant_id=tenant_id,
            top_k=pattern_top_k,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            pattern_types=pattern_types,
            rerank=rerank,
            pattern_document_types=pattern_document_types,
        )
        evidences = self.search(
            query,
            tenant_id=tenant_id,
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
            # 复制，避免 RRF 原地覆盖 vector_results 中保存的原始分数。
            doc_map.setdefault(cid, dict(item))
        for rank, item in enumerate(kw_results):
            cid = item["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in doc_map:
                doc_map[cid] = dict(item)
            elif doc_map[cid].get("similarity") is None and item.get("similarity") is not None:
                # 理论上 vector 先写入；保留此分支防未来调整两路顺序后丢相似度。
                doc_map[cid]["similarity"] = item["similarity"]

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        out = []
        for cid, score in ranked:
            item = doc_map[cid]
            # RRF 只更新融合分，不覆盖原始 similarity。
            item["retrieval_score"] = score
            item["score"] = score  # 兼容旧调用
            item["score_type"] = "rrf"
            out.append(item)
        return out
