"""document 表模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DocumentRecord(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    enterprise_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    project_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    knowledge_base_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    name: Mapped[str | None] = mapped_column(String(512))  # 文档标题(展示用),区别于 file_name
    file_name: Mapped[str] = mapped_column(String(512))
    file_type: Mapped[str] = mapped_column(String(32))  # pdf / docx / doc
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    storage_key: Mapped[str | None] = mapped_column(String(1024))  # 对象存储 key

    # historical_bid / tender / enterprise_profile / qualification / project_case /
    # product_manual / technical_document / regulation / other
    document_type: Mapped[str] = mapped_column(String(64), index=True)
    # pending/parsing/parsed/embedded/failed
    status: Mapped[str] = mapped_column(String(32), default="pending")

    parser: Mapped[str | None] = mapped_column(String(32))  # docx / pdf_text / ocr / mineru
    parser_version: Mapped[str | None] = mapped_column(String(32))
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # SHA256 去重键
    page_count: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
