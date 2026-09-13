"""Reranker(BGE-Reranker,懒加载)。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Reranker:
    """重排候选结果,提升检索精度(跨 chunk 与 pattern 通用)。"""

    def __init__(self) -> None:
        self._model = None
        self._fallback = False

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from FlagEmbedding import FlagReranker

            import torch

            use_fp16 = torch.cuda.is_available()
            self._model = FlagReranker(settings.reranker_model, use_fp16=use_fp16)
            logger.info("Reranker 模型已加载: %s (fp16=%s)", settings.reranker_model, use_fp16)
        except ImportError as exc:
            logger.warning("未安装 reranker 依赖,跳过重排: %s", exc)
            self._fallback = True
        except Exception as exc:  # 模型下载失败/OOM 等:降级为不过滤
            logger.warning("Reranker 加载失败,跳过重排: %s", exc)
            self._fallback = True
        return self._model

    @staticmethod
    def _rerank_text(item: dict[str, Any]) -> str:
        """候选重排文本:pattern 用通用化文本,chunk 用原文。"""
        return (item.get("_rerank_text") or item.get("content") or "").strip()

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

        pairs = [(query, self._rerank_text(c)) for c in candidates]
        scores = model.compute_score(pairs, normalize=True)
        if scores and isinstance(scores[0], (list, tuple)):  # 兼容返回 [[s], ...]
            scores = [s[0] for s in scores]
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda x: float(x[1]), reverse=True)
        for item, score in scored[:top_k]:
            item["score"] = round(float(score), 4)
            item["score_type"] = "rerank"
        return [item for item, _ in scored[:top_k]]
