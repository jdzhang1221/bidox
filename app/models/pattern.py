"""solution_pattern / pattern_source 表模型(可复用方案组件 + 溯源)。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, exists, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.database import Base
from app.core.doc_types import pattern_document_types as resolve_pattern_document_types
from app.models.document import DocumentRecord


def _pattern_vector_type():
    """懒加载 pgvector Vector 类型,未安装时降级 JSONB(检索不可用)。"""
    try:
        from pgvector.sqlalchemy import Vector

        return Vector(settings.vector_dim)
    except ImportError:
        from app.core.logging import get_logger

        get_logger(__name__).warning("未安装 pgvector,solution_pattern.embedding 降级为 JSONB")
        return JSONB()


class SolutionPattern(Base):
    __tablename__ = "solution_pattern"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    enterprise_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    knowledge_base_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    name: Mapped[str] = mapped_column(String(512))
    code: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(64), index=True, default="methodology")
    industry: Mapped[str | None] = mapped_column(String(128))
    scenario: Mapped[str | None] = mapped_column(String(256))

    summary: Mapped[str | None] = mapped_column(Text)
    structure: Mapped[list | None] = mapped_column(JSONB)
    content: Mapped[str | None] = mapped_column(Text)
    # 去事实化正文(LLM 生成):去掉历史项目的具体数字/机构名/人名,用于方案复用,避免污染新标书
    generalized_content: Mapped[str | None] = mapped_column(Text)

    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="active")

    embedding: Mapped[list[float] | None] = mapped_column(_pattern_vector_type())

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatternSource(Base):
    __tablename__ = "pattern_source"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pattern_id: Mapped[int] = mapped_column(BigInteger, index=True)
    document_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    section_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chunk_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # 产生该行的索引版本（来自 parseLogId）。历史数据为 NULL，表示「版本未知」。
    index_version: Mapped[int | None] = mapped_column(BigInteger)

    source_type: Mapped[str] = mapped_column(String(32), default="section")
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def pattern_source_type_clause(document_types: Sequence[str] | None = None):
    """把 `solution_pattern` 限制为「至少有一个来源文档且来源类型在白名单内」（§4.4）。

    为什么不能只过滤 `solution_pattern.tenant_id`：

    - `solution_pattern` 自身没有 `document_type` 列，类型信息只在来源文档上；
    - 存量数据可能已有 tender 来源的 pattern（历史抽取产物），
      只按 tenant 过滤会让招标文件里的要求/评分办法被当成可复用方案注入 QA 上下文。

    实现为 `EXISTS (pattern_source JOIN document ...)`：

    - 文档被删除时 `pattern_source` 行已被 `purge_document_index` 清掉，
      所以「删除即不可召回」天然成立，无需再叠加生命周期条件；
    - 只有来源类型在白名单内的 pattern 才可能命中，tender 来源被确定性排除。

    `document_types=None` 由 `resolve_qa_document_types` 归一化为 QA 白名单 ——
    这样「忘记传参」的默认行为是**安全**的（仍然排除 tender），而不是崩溃或不过滤。

    定义放在 models 层（而非 retrieval 层）是刻意的：models 是叶子，
    反向依赖业务层会形成 `retrieval → knowledge → retrieval` 循环导入。
    """
    allowed = resolve_pattern_document_types(document_types)
    if not allowed:
        # 白名单配置为空 = 不允许任何 pattern 命中（不能退化成「不过滤」）
        return exists(select(1).where(False))
    return exists(
        select(1)
        .select_from(PatternSource)
        .join(DocumentRecord, DocumentRecord.id == PatternSource.document_id)
        .where(
            PatternSource.pattern_id == SolutionPattern.id,
            DocumentRecord.document_type.in_(allowed),
        )
    )
