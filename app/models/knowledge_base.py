"""knowledge_base 表模型(企业知识库边界)。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    enterprise_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)

    # bid_library / enterprise_library / qualification_library / case_library / solution_library
    kind: Mapped[str] = mapped_column(String(64), index=True, default="bid_library")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
