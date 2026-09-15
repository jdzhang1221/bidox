"""LLM 客户端抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LLMStreamChunk:
    """流式增量。text 与 finish_reason 至少一个有值。"""

    text: str = ""
    finish_reason: str | None = None


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

    @abstractmethod
    async def stream_chat(
        self, messages: list[dict], timeout: float | None = None
    ) -> AsyncIterator[LLMStreamChunk]:
        """真正的异步增量流。首个增量后禁止内部自动重试，避免重复文本。"""
        if False:  # pragma: no cover - 让类型检查器识别为 async generator
            yield LLMStreamChunk()
        raise NotImplementedError
