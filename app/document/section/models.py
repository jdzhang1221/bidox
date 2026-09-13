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

    # 展平属性(赋值时由 detector / flatten 填充)
    document_id: int | None = None
    id: int | None = None  # 临时 id(展平序列下标),chunk 用它记录来源章节,入库后映射到 document_section.id
    parent_id: int | None = None  # 临时父 id(展平序列下标),NULL=顶层章节
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

    @property
    def full_content(self) -> str:
        """章节全文(含子章节),供方案组件抽取等需要完整上下文的场景。"""
        parts: list[str] = []
        own = self.content
        if own:
            parts.append(own)
        for child in self.children:
            child_text = child.full_content
            if child_text:
                parts.append(f"## {child.title}\n{child_text}")
        return "\n".join(parts)

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


def flatten_sections_with_parent(sections: list["Section"]) -> list["Section"]:
    """DFS 展平章节树,并回填临时 id / 父 id(入库映射真实主键的唯一真源)。

    - id: 节点在展平序列中的下标(临时 id)。chunk 用它记录来源章节,入库后映射为真实主键。
    - parent_id: 父节点临时 id,NULL=顶层章节。
    确定性 DFS,多次调用结果一致(幂等)。
    """
    flat: list["Section"] = []

    def walk(sec: "Section", parent_id: int | None) -> None:
        idx = len(flat)
        flat.append(sec)
        sec.id = idx
        sec.parent_id = parent_id
        for child in sec.children:
            walk(child, idx)

    for s in sections:
        walk(s, None)
    return flat
