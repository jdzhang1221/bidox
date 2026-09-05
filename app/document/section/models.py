"""Section 章节模型(树形结构)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Section(BaseModel):
    """文档章节节点。"""

    title: str
    level: int = 1
    section_no: str | None = None
    section_type: str | None = None

    # 树结构
    children: list["Section"] = Field(default_factory=list)

    # 章节下的内容块(直接归属该层级、非子章节的块)
    content_blocks: list = Field(default_factory=list)

    page_start: int | None = None
    page_end: int | None = None

    # 展平属性(赋值时由 detector 填充)
    document_id: int | None = None
    parent_id: int | None = None
    sort_order: int = 0

    @property
    def content(self) -> str:
        """章节正文文本(不含子章节)。"""
        parts = []
        for b in self.content_blocks:
            if hasattr(b, "text"):
                parts.append(b.text)
            elif hasattr(b, "to_markdown"):
                parts.append(b.to_markdown())
            elif hasattr(b, "to_text"):
                parts.append(b.to_text())
        return "\n".join(p for p in parts if p)

    def to_dict(self) -> dict:
        """转成 document_section 表记录。"""
        return {
            "title": self.title,
            "level": self.level,
            "section_no": self.section_no,
            "section_type": self.section_type,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "content": self.content,
            "parent_id": self.parent_id,
            "sort_order": self.sort_order,
        }

    def walk(self):
        """深度优先遍历所有节点(含自身)。"""
        yield self
        for child in self.children:
            yield from child.walk()
