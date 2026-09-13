"""LLM 客户端抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(Exception):
    """LLM 调用失败。"""


class BaseLLMClient(ABC):
    """统一 LLM 接口(OpenAI 兼容)。"""

    @abstractmethod
    def complete(self, prompt: str, system: str | None = None, timeout: float | None = None) -> str:
        """补全(非流式)。timeout 为单次调用超时,缺省用配置值。"""
        raise NotImplementedError

    @abstractmethod
    def chat(self, messages: list[dict], json_mode: bool = False, timeout: float | None = None) -> str:
        """多轮对话。timeout 为单次调用超时,缺省用配置值。"""
        raise NotImplementedError
