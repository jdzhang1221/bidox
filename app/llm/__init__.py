"""LLM 网关:统一接入 deepseek / qwen / openai(OpenAI 兼容协议)。"""

from app.llm.gateway import LLMGateway, get_llm

__all__ = ["LLMGateway", "get_llm"]
