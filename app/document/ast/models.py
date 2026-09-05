"""Document AST 模型。

所有解析器(PDF/Word/OCR/MinerU)统一输出此结构,后续 Section/Chunk/检索/AI 全基于 AST。

结构:
    Document
      ├── Heading      (标题,含 level)
      ├── Paragraph    (段落)
      ├── Table        (表格,含 rows)
      ├── ListBlock    (列表)
      ├── Image        (图片)
      └── Caption      (图表题注)
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# 阅读顺序中的块类型
BlockType = Literal["heading", "paragraph", "table", "list", "image", "caption"]

# 边界框 [x0, y0, x1, y1],PDF 用,Word 可忽略
BBox = tuple[float, float, float, float] | None


class _BaseBlock(BaseModel):
    """所有块的公共字段。"""

    page: int | None = None
    bbox: BBox = None


class Heading(_BaseBlock):
    type: Literal["heading"] = "heading"
    level: int = 1
    text: str
    # 编号,如 "5.1.2" / "第五章" / None
    section_no: str | None = None


class Paragraph(_BaseBlock):
    type: Literal["paragraph"] = "paragraph"
    text: str
    # Word 样式名 / PDF 字体信息
    style: str | None = None


class Table(_BaseBlock):
    type: Literal["table"] = "table"
    rows: list[list[str]] = Field(default_factory=list)
    caption: str | None = None

    def to_markdown(self) -> str:
        """表格转 Markdown(保留行列结构,供 embedding / LLM 使用)。"""
        if not self.rows:
            return ""
        lines = []
        header = self.rows[0]
        lines.append("| " + " | ".join(_esc(c) for c in header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for row in self.rows[1:]:
            lines.append("| " + " | ".join(_esc(c) for c in row) + " |")
        return "\n".join(lines)


class ListBlock(_BaseBlock):
    type: Literal["list"] = "list"
    items: list[str] = Field(default_factory=list)
    ordered: bool = False

    def to_text(self) -> str:
        prefix = "1. " if self.ordered else "- "
        return "\n".join(f"{prefix}{it}" for it in self.items)


class Image(_BaseBlock):
    type: Literal["image"] = "image"
    # 图片引用:字节 / 本地路径 / 对象 key
    source: str | None = None
    alt: str | None = None


class Caption(_BaseBlock):
    type: Literal["caption"] = "caption"
    text: str


Block = Annotated[
    Union[Heading, Paragraph, Table, ListBlock, Image, Caption],
    Field(discriminator="type"),
]


class Document(BaseModel):
    """文档 AST 根节点。"""

    type: Literal["document"] = "document"
    # 来源信息
    source: str | None = None
    file_type: str | None = None
    title: str | None = None

    # 按阅读顺序排列的块列表
    blocks: list[Block] = Field(default_factory=list)

    # 解析器自定义元信息(如 OCR 置信度、MinerU 结构质量分等)
    meta: dict = Field(default_factory=dict)

    @property
    def headings(self) -> list[Heading]:
        return [b for b in self.blocks if isinstance(b, Heading)]

    @property
    def tables(self) -> list[Table]:
        return [b for b in self.blocks if isinstance(b, Table)]

    @property
    def page_count(self) -> int:
        pages = {b.page for b in self.blocks if b.page is not None}
        return max(pages) if pages else 0

    def iter_text_blocks(self) -> list[str]:
        """抽取所有可读文本(段落/标题/表格/列表/题注),用于质量检测。"""
        parts: list[str] = []
        for b in self.blocks:
            if isinstance(b, (Heading, Paragraph, Caption)):
                parts.append(b.text)
            elif isinstance(b, Table):
                parts.append(b.to_markdown())
            elif isinstance(b, ListBlock):
                parts.append(b.to_text())
        return parts


def _esc(cell: str) -> str:
    """转义 Markdown 表格单元格中的竖线。"""
    return cell.replace("|", "\\|").replace("\n", " ")
