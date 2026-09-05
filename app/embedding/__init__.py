"""向量化:Embedding 抽象 + BGE-M3(本地/Ollama) + 批量服务。"""

from app.embedding.base import BaseEmbedder, EmbeddingError
from app.embedding.bge_m3 import BgeM3Embedder
from app.embedding.ollama import OllamaEmbedder
from app.embedding.service import EmbeddingService, create_embedder

__all__ = [
    "BaseEmbedder",
    "EmbeddingError",
    "BgeM3Embedder",
    "OllamaEmbedder",
    "EmbeddingService",
    "create_embedder",
]
