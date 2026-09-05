"""Reranker(BGE-Reranker,懒加载)。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Reranker:
    """重排候选结果,提升检索精度。"""

    def __init__(self) -> None:
        self._model = None
        self._fallback = False

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from FlagEmbedding import FlagReranker

            self._model = FlagReranker(settings.reranker_model)
            logger.info("Reranker 模型已加载: %s", settings.reranker_model)
        except ImportError as exc:
            logger.warning("未安装 reranker 依赖,跳过重排: %s", exc)
            self._fallback = True
        return self._model

    def rerank(
        self, query: str, candidates: list[dict[str, Any]], top_k: int | None = None
    ) -> list[dict[str, Any]]:
        """重排候选,返回 top_k。"""
        if not candidates:
            return []
        top_k = top_k or settings.reranker_top_k
        model = self._get_model()
        if self._fallback or model is None:
            return candidates[:top_k]

        pairs = [(query, c["content"]) for c in candidates]
        scores = model.compute_score(pairs, normalize=True)
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
        for item, score in scored[:top_k]:
            item["score"] = float(score)
        return [item for item, _ in scored[:top_k]]
