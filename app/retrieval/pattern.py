"""方案组件(Pattern)向量检索:方案级召回。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from app.core.database import session_scope
from app.core.doc_types import pattern_document_types
from app.core.logging import get_logger
from app.core.tenant import require_tenant_id
from app.models.pattern import SolutionPattern, pattern_source_type_clause

logger = get_logger(__name__)


def _rerank_text(p: SolutionPattern) -> str:
    """拼接通用化重排文本(name+type+industry+scenario+summary+structure)。"""
    parts = [
        p.name or "",
        p.type or "",
        p.industry or "",
        p.scenario or "",
        p.summary or "",
    ]
    if p.structure:
        parts.append(" / ".join(str(s) for s in p.structure))
    return " ".join(x for x in parts if x)


class PatternRetriever:
    """pgvector 方案组件相似度检索。

    embedding 内容为 name+type+industry+scenario+summary+structure,更适合方案级召回。
    """

    def search(
        self,
        query_vector: list[float],
        tenant_id: int,
        top_k: int = 5,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        pattern_types: list[str] | None = None,
        status: str = "active",
        document_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """返回 top_k 条方案组件,含相似度。

        pattern_types: 命中意图时只召回指定类型(受控枚举,见 PATTERN_TYPES)。

        document_types: **来源文档类型白名单**,默认取 QA 白名单（排除 tender）。
        这不是可选项 —— `solution_pattern` 没有 document_type 列，只过滤 tenant
        会让招标文件（tender）来源的方案组件进入问答上下文（§4.4）。
        """
        tenant_id = require_tenant_id(tenant_id, where="PatternRetriever.search")
        allowed_doc_types = pattern_document_types(document_types)
        with session_scope() as session:
            stmt = select(
                SolutionPattern,
                (SolutionPattern.embedding.cosine_distance(query_vector)).label("distance"),
            ).where(SolutionPattern.embedding.is_not(None))
            # 租户是硬边界:不选知识库只是不加 knowledge_base_id 条件,租户条件永远存在。
            stmt = stmt.where(SolutionPattern.tenant_id == tenant_id)
            # 来源类型是第二道硬边界:必须能追到一个白名单类型的来源文档。
            stmt = stmt.where(pattern_source_type_clause(allowed_doc_types))
            if enterprise_id is not None:
                stmt = stmt.where(SolutionPattern.enterprise_id == enterprise_id)
            if knowledge_base_id is not None:
                stmt = stmt.where(SolutionPattern.knowledge_base_id == knowledge_base_id)
            if pattern_types:
                stmt = stmt.where(SolutionPattern.type.in_(pattern_types))
            if status:
                stmt = stmt.where(SolutionPattern.status == status)
            stmt = stmt.order_by(text("distance")).limit(top_k)
            rows = session.execute(stmt).all()

        return [
            {
                "pattern_id": row.SolutionPattern.id,
                "code": row.SolutionPattern.code,
                "name": row.SolutionPattern.name,
                "type": row.SolutionPattern.type,
                "industry": row.SolutionPattern.industry,
                "scenario": row.SolutionPattern.scenario,
                "summary": row.SolutionPattern.summary,
                "structure": row.SolutionPattern.structure or [],
                "content": row.SolutionPattern.content,
                "generalized_content": row.SolutionPattern.generalized_content,
                "fingerprint": row.SolutionPattern.fingerprint,
                "knowledge_base_id": row.SolutionPattern.knowledge_base_id,
                "score": round(1 - row.distance, 4),
                "score_type": "cosine",
                # 重排文本用通用化表示(不掺原始事实)
                "_rerank_text": _rerank_text(row.SolutionPattern),
            }
            for row in rows
        ]
