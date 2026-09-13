"""方案组件(Pattern)向量检索:方案级召回。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.pattern import SolutionPattern

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
        top_k: int = 5,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        pattern_types: list[str] | None = None,
        status: str = "active",
    ) -> list[dict[str, Any]]:
        """返回 top_k 条方案组件,含相似度。

        pattern_types: 命中意图时只召回指定类型(受控枚举,见 PATTERN_TYPES)。
        """
        with session_scope() as session:
            stmt = select(
                SolutionPattern,
                (SolutionPattern.embedding.cosine_distance(query_vector)).label("distance"),
            ).where(SolutionPattern.embedding.is_not(None))
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
