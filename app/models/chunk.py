"""document_chunk 表模型(含 pgvector 向量)。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.database import Base


def _vector_type():
    """懒加载 pgvector Vector 类型。

    未安装 pgvector 时降级为 JSONB,保证骨架可 import(但真实向量检索需 pgvector)。
    """
    try:
        from pgvector.sqlalchemy import Vector

        return Vector(settings.vector_dim)
    except ImportError:
        from app.core.logging import get_logger

        get_logger(__name__).warning("未安装 pgvector,embedding 字段降级为 JSONB(检索不可用)")
        return JSONB()


class DocumentChunk(Base):
    __tablename__ = "document_chunk"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    enterprise_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    knowledge_base_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    document_id: Mapped[int] = mapped_column(BigInteger, index=True)
    section_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # 产生该行的索引版本（来自 parseLogId）。历史数据为 NULL，表示「版本未知」。
    index_version: Mapped[int | None] = mapped_column(BigInteger)

    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    # paragraph / table / list / heading / mixed
    chunk_type: Mapped[str] = mapped_column(String(32), default="paragraph")

    title: Mapped[str | None] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)

    # tender / historical_bid / ...
    document_type: Mapped[str | None] = mapped_column(String(64), index=True)
    section_type: Mapped[str | None] = mapped_column(String(64), index=True)

    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    # 列名保留 "metadata",但属性名不能用 metadata(Declarative 保留字)
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    embedding: Mapped[list[float] | None] = mapped_column(_vector_type())

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
