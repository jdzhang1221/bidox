"""知识库查询服务:对外暴露统一检索入口。"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.retrieval.hybrid import HybridRetriever

logger = get_logger(__name__)


class KnowledgeService:
    """知识库统一查询。"""

    def __init__(self) -> None:
        self._retriever = HybridRetriever()

    def search(
        self,
        query: str,
        top_k: int | None = None,
        document_type: str | None = None,
        enterprise_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """混合检索,返回带可追溯信息的 chunk 列表。"""
        return self._retriever.search(
            query, top_k=top_k, document_type=document_type, enterprise_id=enterprise_id
        )

    def build_context(self, query: str, top_k: int | None = None) -> str:
        """把检索结果拼成 evidence context(供 LLM 使用)。"""
        results = self.search(query, top_k=top_k)
        parts = []
        for i, r in enumerate(results, 1):
            source = f"文档#{r['document_id']}"
            if r.get("page_start"):
                source += f" 第{r['page_start']}页"
            if r.get("section_type"):
                source += f" [{r['section_type']}]"
            parts.append(f"[依据{i}] {source}\n{r['content']}")
        return "\n\n".join(parts)
