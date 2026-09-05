"""Embedding 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingError(Exception):
    """向量化失败。"""


class BaseEmbedder(ABC):
    """文本向量化接口。"""

    #: 向量维度
    dim: int = 0
    #: 模型名
    model_name: str = ""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化,返回与输入等长的向量列表。"""
        raise NotImplementedError

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """查询向量化(单条)。"""
        raise NotImplementedError
