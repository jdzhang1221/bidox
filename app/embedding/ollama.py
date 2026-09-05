"""Ollama 向量化实现:通过 Ollama HTTP API 调用本地 embedding 模型(如 bge-m3)。"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.embedding.base import BaseEmbedder, EmbeddingError

logger = get_logger(__name__)


class OllamaEmbedder(BaseEmbedder):
    """通过 Ollama `/api/embed` 调用本地 embedding 模型。

    依赖 Ollama 已启动并已 `ollama pull bge-m3`。
    """

    model_name = settings.ollama_embedding_model
    dim = settings.vector_dim

    def __init__(self) -> None:
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._timeout = settings.ollama_timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = httpx.post(
                f"{self._base_url}/api/embed",
                json={"model": self.model_name, "input": texts},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Ollama 调用失败({self._base_url}): {exc}") from exc

        vectors = payload.get("embeddings", [])
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"Ollama 返回向量数量不符: 期望 {len(texts)}, 实际 {len(vectors)}"
            )

        result: list[list[float]] = []
        for v in vectors:
            if len(v) != self.dim:
                raise EmbeddingError(
                    f"向量维度不符: 期望 {self.dim}, 实际 {len(v)}。"
                    f"请确认 Ollama 模型为 bge-m3(1024 维),并同步 VECTOR_DIM"
                )
            result.append(list(v))
        return result

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]
