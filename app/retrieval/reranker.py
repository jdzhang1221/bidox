"""Reranker(BGE-Reranker,懒加载)。"""

from __future__ import annotations

import threading
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 进程级模型缓存。
#
# 为什么必须放在模块级而不是实例上：`KnowledgeService()` 是**每请求新建**的
# （`app/api/knowledge.py` 与 `app/knowledge/qa_stream.py` 都直接 `KnowledgeService()`），
# 它内部又新建 `HybridRetriever()` → `Reranker()`。若模型挂在实例上，
# 每个问答请求都会重新加载一次 reranker 模型（本机实测冷启动可达 1~2 分钟，
# 且离线时还要先等若干次 huggingface HEAD 超时），请求会在「还没开始检索」的阶段就耗光预算。
#
# 缓存值语义：`None` 表示**加载失败**，也一并缓存。失败通常是「依赖未安装 / 模型缺失」这类
# 不会因重试而改变的原因；不缓存失败就会让每个请求都重付一次超时代价。
_MODEL_CACHE: dict[str, Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()


class Reranker:
    """重排候选结果,提升检索精度(跨 chunk 与 pattern 通用)。"""

    def __init__(self) -> None:
        self._fallback = False

    def _get_model(self):
        key = settings.reranker_model
        with _MODEL_CACHE_LOCK:
            if key in _MODEL_CACHE:
                model = _MODEL_CACHE[key]
            else:
                model = self._load_model()
                _MODEL_CACHE[key] = model
        self._fallback = model is None
        return model

    def _load_model(self):
        """真正加载模型。只在缓存未命中时调用，调用方持有 `_MODEL_CACHE_LOCK`。

        加载可能持续很久，因此这里**故意**让并发首次请求串行等待：并发加载同一个 2GB 模型
        既浪费内存又可能 OOM，串行等待是更安全的选择。首次加载完成后不再有竞争。
        """
        try:
            from FlagEmbedding import FlagReranker

            import torch

            use_fp16 = torch.cuda.is_available()
            model = FlagReranker(settings.reranker_model, use_fp16=use_fp16)
            logger.info("Reranker 模型已加载: %s (fp16=%s)", settings.reranker_model, use_fp16)
            return model
        except ImportError as exc:
            logger.warning("未安装 reranker 依赖,跳过重排: %s", exc)
            return None
        except Exception as exc:  # 模型下载失败/OOM 等:降级为不过滤
            logger.warning("Reranker 加载失败,跳过重排: %s", exc)
            return None

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
            # reranker 只更新最终检索分；向量原始 similarity 必须原样保留供引用解释。
            retrieval_score = round(float(score), 4)
            item["retrieval_score"] = retrieval_score
            item["score"] = retrieval_score  # 兼容旧调用
            item["score_type"] = "reranker"
        return [item for item, _ in scored[:top_k]]
