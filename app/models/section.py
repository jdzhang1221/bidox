"""document_section 表模型(章节树)。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DocumentSection(Base):
    __tablename__ = "document_section"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(BigInteger, index=True)
    knowledge_base_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # 产生该行的索引版本（来自 parseLogId）。历史数据为 NULL，表示「版本未知」。
    index_version: Mapped[int | None] = mapped_column(BigInteger)

    title: Mapped[str] = mapped_column(String(512))
    level: Mapped[int] = mapped_column(Integer, default=1)
    section_no: Mapped[str | None] = mapped_column(String(64))  # 如 5.1.2
    # technical_solution / system_architecture / score_criteria ...
    section_type: Mapped[str | None] = mapped_column(String(64), index=True)

    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text)

    # 列名保留 "metadata",属性名用 meta 避开 Declarative 保留字
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
