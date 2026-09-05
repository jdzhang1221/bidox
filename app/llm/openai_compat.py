"""OpenAI 兼容协议客户端(deepseek / qwen / openai 均支持)。

用 httpx 直接调用 /chat/completions,避免额外 SDK 依赖。
"""

from __future__ import annotations

import json

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import BaseLLMClient, LLMError

logger = get_logger(__name__)


class OpenAICompatClient(BaseLLMClient):
    """通用 OpenAI 兼容客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"LLM 调用失败: {exc.response.status_code} {exc.response.text}") from exc

    def chat(self, messages: list[dict], json_mode: bool = False) -> str:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.llm_temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post(payload)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM 返回格式异常: {data}") from exc

    def complete(self, prompt: str, system: str | None = None) -> str:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages)

    def complete_json(self, prompt: str, system: str | None = None) -> dict:
        """返回 JSON 对象(要求模型输出 json_object)。"""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        raw = self.chat(messages, json_mode=True)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """容错解析 JSON(剔除代码块围栏)。"""
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM 输出非合法 JSON: {raw[:200]}") from exc
