"""Chunk 模型(对应 document_chunk 表)。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """一个知识切片。"""

    document_id: int | None = None
    section_id: int | None = None
    chunk_index: int = 0

    # 标题:继承自所属章节(如 "5.1.2 数据架构")
    title: str | None = None
    content: str

    document_type: str | None = None
    section_type: str | None = None

    page_start: int | None = None
    page_end: int | None = None

    # 附加元信息(是否表格、原始块类型等)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # 向量(由 embedding 阶段填充,不在切分时生成)
    embedding: list[float] | None = None

    def to_db_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "section_id": self.section_id,
            "chunk_index": self.chunk_index,
            "title": self.title,
            "content": self.content,
            "document_type": self.document_type,
            "section_type": self.section_type,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "meta": self.metadata,
        }
