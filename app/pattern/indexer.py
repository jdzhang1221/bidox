"""方案组件索引:抽取 + 向量化 + 写入 solution_pattern / pattern_source。"""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.database import session_scope
from app.core.logging import get_logger
from app.document.section.models import Section
from app.embedding.service import EmbeddingService
from app.models.pattern import PatternSource, SolutionPattern as SolutionPatternRecord
from app.pattern.extractor import SolutionPatternExtractor
from app.pattern.models import SolutionPattern

logger = get_logger(__name__)


def _fingerprint(text: str) -> str:
    """内容指纹(通用化哈希),跨文档去重键。"""
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


def _embedding_text(p: SolutionPattern) -> str:
    """Pattern 向量化文本:name+type+industry+scenario+summary+structure。"""
    structure = "\n".join(f"- {s}" for s in (p.structure or []))
    parts = [
        f"名称：{p.name}",
        f"类型：{p.type}",
        f"行业：{p.industry}",
        f"场景：{p.scenario}",
        f"摘要：{p.summary}",
    ]
    if structure:
        parts.append(f"结构：\n{structure}")
    return "\n".join(parts)


class SolutionPatternIndexer:
    """方案组件索引器(抽取 → 向量化 → 落库)。"""

    def __init__(self, extractor: SolutionPatternExtractor | None = None) -> None:
        self._extractor = extractor or SolutionPatternExtractor()
        self._embedding = EmbeddingService()

    def extract(
        self,
        result: Any,
        doc_meta: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> list[tuple[SolutionPattern, Section]]:
        """从 ParseResult 抽取候选方案组件,返回 (pattern, source_section) 列表。"""
        candidates = SolutionPatternExtractor.select_candidates(result.sections)
        pairs: list[tuple[SolutionPattern, Section]] = []
        for i, sec in enumerate(candidates, 1):
            pattern = self._extractor.extract(
                sec, code=f"SP{i:03d}", doc_meta=doc_meta or {}, timeout=timeout
            )
            pairs.append((pattern, sec))
        logger.info("方案组件抽取完成: %d 个", len(pairs))
        return pairs

    def save(
        self,
        pairs: list[tuple[SolutionPattern, Section]],
        *,
        id_map: dict[int, int],
        document_id: int,
        knowledge_base_id: int | None = None,
        tenant_id: int | None = None,
        enterprise_id: int | None = None,
    ) -> int:
        """向量化并写入 solution_pattern + pattern_source。返回 pattern 数。"""
        if not pairs:
            return 0

        patterns = [p for p, _ in pairs]
        texts = [_embedding_text(p) for p in patterns]
        vectors = self._embedding.embed_documents(texts)

        with session_scope() as session:
            for (p, sec), vector in zip(pairs, vectors):
                record = SolutionPatternRecord(
                    tenant_id=tenant_id,
                    enterprise_id=enterprise_id,
                    knowledge_base_id=knowledge_base_id,
                    name=p.name,
                    code=p.code,
                    type=p.type,
                    industry=p.industry or None,
                    scenario=p.scenario or None,
                    summary=p.summary or None,
                    structure=p.structure or None,
                    content=p.content or None,
                    generalized_content=p.generalized_content or None,
                    fingerprint=_fingerprint(p.summary or p.name),
                    version=1,
                    status="active",
                    embedding=vector,
                )
                session.add(record)
                session.flush()

                session.add(
                    PatternSource(
                        pattern_id=record.id,
                        document_id=document_id,
                        section_id=id_map.get(sec.id),
                        chunk_id=None,
                        source_type="section",
                        page_start=sec.page_start,
                        page_end=sec.page_end,
                        sort_order=0,
                    )
                )
        logger.info("方案组件入库完成: %d 个", len(pairs))
        return len(pairs)
