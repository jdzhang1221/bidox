"""方案组件索引:抽取 + 向量化 + 写入 solution_pattern / pattern_source。"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import session_scope
from app.core.doc_types import is_pattern_eligible, normalize_document_type
from app.core.logging import get_logger
from app.document.section.models import Section
from app.embedding.service import EmbeddingService
from app.knowledge.guard import complete_pattern_job, pattern_job_is_current
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
        max_workers: int | None = None,
        document_type: str | None = None,
    ) -> list[tuple[SolutionPattern, Section]]:
        """从 ParseResult 抽取候选方案组件,返回 (pattern, source_section) 列表。

        `document_type` 不属于方案组件白名单（如 tender）时**直接返回空**，
        连 LLM 都不调 —— 逐章节抽取是整条链路里最贵的一步，不该为注定要丢弃的文档付费（§4.4）。

        逐章节调 LLM 是网络 IO 密集操作:单篇 150+ 章节标书串行需 10 分钟以上。
        因此用**有界线程池**并发执行(LLM 客户端每次调用都新建 httpx.Client,
        无共享连接池/可变状态,天然线程安全)。

        - code(SP001/SP002/…)按候选章节的**原始序号**预分配,并发不改变编号,
          返回顺序也与 candidates 一致 —— 入库结果与串行版完全相同。
        - 单章节失败只跳过该章节(与 scripts/extract_solution_patterns.py 策略一致),
          不再让一个章节的超时/限流把整篇已成功的组件全部丢掉。
          失败会在 code 上留下空洞(如 SP001、SP003),这是刻意保留的:
          code 始终对应候选章节位置,便于回溯到原文。
        """
        normalized_type = normalize_document_type(document_type)
        if not is_pattern_eligible(normalized_type):
            logger.info(
                "文档类型 %s 不属于方案组件可用类型，跳过抽取（不调用 LLM）", normalized_type
            )
            return []

        candidates = SolutionPatternExtractor.select_candidates(result.sections)
        if not candidates:
            logger.info("方案组件抽取完成: 0 个(无候选章节)")
            return []

        workers = max_workers or settings.pattern_extract_concurrency
        workers = max(1, min(workers, len(candidates)))
        meta = doc_meta or {}

        slots: list[tuple[SolutionPattern, Section] | None] = [None] * len(candidates)
        if workers == 1:
            for i, sec in enumerate(candidates):
                slots[i] = self._extract_one(sec, i + 1, meta, timeout)
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="pattern-extract"
            ) as pool:
                future_to_idx = {
                    pool.submit(self._extract_one, sec, i + 1, meta, timeout): i
                    for i, sec in enumerate(candidates)
                }
                for future in as_completed(future_to_idx):
                    slots[future_to_idx[future]] = future.result()

        pairs = [slot for slot in slots if slot is not None]
        logger.info(
            "方案组件抽取完成: %d 个(候选 %d,失败 %d,并发 %d)",
            len(pairs),
            len(candidates),
            len(candidates) - len(pairs),
            workers,
        )
        return pairs

    def _extract_one(
        self,
        section: Section,
        seq: int,
        doc_meta: dict[str, Any],
        timeout: float | None,
    ) -> tuple[SolutionPattern, Section] | None:
        """抽取单个章节;失败返回 None,由调用方跳过(不中断整篇)。"""
        code = f"SP{seq:03d}"
        try:
            pattern = self._extractor.extract(
                section, code=code, doc_meta=doc_meta, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - 单节失败降级,不影响其余章节
            logger.warning("方案组件抽取失败 %s %s: %s", code, section.title, exc)
            return None
        return pattern, section

    def save(
        self,
        pairs: list[tuple[SolutionPattern, Section]],
        *,
        id_map: dict[int, int],
        document_id: int,
        knowledge_base_id: int | None = None,
        tenant_id: int | None = None,
        enterprise_id: int | None = None,
        index_version: int | None = None,
        owner_id: UUID | None = None,
        session: Session | None = None,
        document_type: str | None = None,
    ) -> int:
        """向量化并写入 solution_pattern + pattern_source。返回 pattern 数。

        传入 `index_version` + `owner_id` 时启用**任务归属校验**：
        写库前确认 guard 的已提交版本就是本次版本、且 pattern job 仍归自己持有。
        不满足则整批丢弃（返回 0）—— 这是「V1 抽取跑完时 V2 已提交」的正确处理，
        避免旧版本任务把 pattern 写进新版本的索引。

        `document_type` 是 §4.4 的**持久化层强制拒绝**：非白名单类型（tender）整批丢弃。
        这是最后一道闸门 —— 无论调用方是谁、传了什么，招标文件都进不了方案库。
        """
        if not pairs:
            return 0

        normalized_type = normalize_document_type(document_type)
        if not is_pattern_eligible(normalized_type):
            logger.warning(
                "文档 %s 类型为 %s，不属于方案组件可用类型，丢弃本次 %d 个候选，不写入",
                document_id,
                normalized_type,
                len(pairs),
            )
            return 0

        patterns = [p for p, _ in pairs]
        texts = [_embedding_text(p) for p in patterns]
        vectors = self._embedding.embed_documents(texts)

        context = nullcontext(session) if session is not None else session_scope()
        with context as db_session:
            if index_version is not None and owner_id is not None:
                if tenant_id is None:
                    raise ValueError("带 index_version 保存 pattern 时必须提供 tenant_id")
                if not pattern_job_is_current(
                    db_session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    index_version=index_version,
                    owner_id=owner_id,
                ):
                    logger.warning(
                        "文档 %s 版本 %s 的 pattern 任务已失效（更高版本已提交或任务被接管），"
                        "丢弃本次 %d 个候选，不写入",
                        document_id,
                        index_version,
                        len(pairs),
                    )
                    return 0

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
                db_session.add(record)
                db_session.flush()

                db_session.add(
                    PatternSource(
                        pattern_id=record.id,
                        document_id=document_id,
                        section_id=id_map.get(sec.id),
                        chunk_id=None,
                        index_version=index_version,
                        source_type="section",
                        page_start=sec.page_start,
                        page_end=sec.page_end,
                        sort_order=0,
                    )
                )

            # pattern 写入 + job COMPLETED 必须同事务，否则重试会重复写入。
            if index_version is not None and owner_id is not None:
                complete_pattern_job(
                    db_session,
                    tenant_id=tenant_id,  # type: ignore[arg-type]
                    document_id=document_id,
                    index_version=index_version,
                    owner_id=owner_id,
                )
        logger.info("方案组件入库完成: %d 个（index_version=%s）", len(pairs), index_version)
        return len(pairs)

    @staticmethod
    def complete_job(
        session: Session,
        *,
        document_id: int,
        tenant_id: int,
        index_version: int,
        owner_id: UUID,
    ) -> bool:
        """没有候选章节时，也要把任务收尾成 COMPLETED，避免每次触发都重跑。"""
        return complete_pattern_job(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            index_version=index_version,
            owner_id=owner_id,
        )
