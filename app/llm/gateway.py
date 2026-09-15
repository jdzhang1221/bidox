"""LLM 网关:按 provider 分发客户端。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import BaseLLMClient, LLMStreamChunk
from app.llm.openai_compat import OpenAICompatClient

logger = get_logger(__name__)

# 各 provider 默认 base_url
_PROVIDER_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai": "https://api.openai.com/v1",
}

# 各 provider 默认模型(未显式配置时)
_PROVIDER_MODELS = {
    "deepseek": "deepseek-chat",
    "qwen": "qwen-max",
    "openai": "gpt-4o",
}


class LLMGateway:
    """LLM 统一入口。"""

    def __init__(self, provider: str | None = None) -> None:
        self.provider = (provider or settings.llm_provider).lower()
        self._client = self._build()

    def _build(self) -> BaseLLMClient:
        base_url = settings.llm_base_url or _PROVIDER_URLS.get(self.provider, "")
        model = settings.llm_model or _PROVIDER_MODELS.get(self.provider, "")
        logger.info("LLM 网关: provider=%s model=%s", self.provider, model)
        return OpenAICompatClient(base_url=base_url, api_key=settings.llm_api_key, model=model)

    @property
    def client(self) -> BaseLLMClient:
        return self._client

    def complete(self, prompt: str, system: str | None = None, timeout: float | None = None) -> str:
        return self._client.complete(prompt, system, timeout=timeout)

    def complete_json(self, prompt: str, system: str | None = None, timeout: float | None = None) -> dict:
        """结构化输出(JSON)。"""
        if hasattr(self._client, "complete_json"):
            return self._client.complete_json(prompt, system, timeout=timeout)  # type: ignore[union-attr]
        return self._client.complete(prompt, system, timeout=timeout)  # type: ignore[return-value]

    async def stream_chat(
        self, messages: list[dict], timeout: float | None = None
    ) -> AsyncIterator[LLMStreamChunk]:
        async for chunk in self._client.stream_chat(messages, timeout=timeout):
            yield chunk


@lru_cache
def get_llm() -> LLMGateway:
    return LLMGateway()
