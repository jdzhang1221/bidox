"""BGE-M3 向量化实现(懒加载 FlagEmbedding / sentence-transformers)。"""

from __future__ import annotations

import hashlib

from app.core.config import settings
from app.core.logging import get_logger
from app.embedding.base import BaseEmbedder, EmbeddingError

logger = get_logger(__name__)


class BgeM3Embedder(BaseEmbedder):
    """BGE-M3(1024 维)。

    未安装模型依赖时,若 `embedding_required=False`,降级为确定性 hash 向量,
    保证开发阶段主链路可跑通(但检索语义会退化)。
    """

    model_name = settings.embedding_model
    dim = settings.vector_dim

    def __init__(self) -> None:
        self._model = None
        self._fallback = False

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from FlagEmbedding import BGEM3FlagModel

            self._model = BGEM3FlagModel(
                settings.embedding_model,
                use_fp16=settings.embedding_device == "cuda",
                device=settings.embedding_device,
            )
            logger.info("BGE-M3 模型已加载: %s", settings.embedding_model)
        except ImportError as exc:
            if settings.embedding_required:
                raise EmbeddingError(
                    "未安装 embedding 依赖: pip install -e '.[embedding]'"
                ) from exc
            logger.warning("未安装 embedding 依赖,降级为 hash 向量(stub)")
            self._fallback = True
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        if self._fallback or model is None:
            return [self._hash_embed(t) for t in texts]
        # BGE-M3 dense 向量
        output = model.encode(texts, batch_size=settings.embedding_batch_size)
        return [v.tolist() for v in output["dense_vecs"]]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @staticmethod
    def _hash_embed(text: str) -> list[float]:
        """确定性 hash 向量(降级 stub)。"""
        vec = [0.0] * settings.vector_dim
        for i in range(settings.vector_dim):
            digest = hashlib.md5(f"{text}:{i}".encode()).digest()
            vec[i] = (digest[0] - 128) / 128.0
        # L2 归一化
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]
