"""Document AST 构建器:解析器用其逐个追加块。"""

from __future__ import annotations

from app.document.ast.models import (
    Caption,
    Document,
    Heading,
    Image,
    ListBlock,
    Paragraph,
    Table,
)


class DocumentBuilder:
    """流式构建 Document AST。"""

    def __init__(self, source: str | None = None, file_type: str | None = None) -> None:
        self._doc = Document(source=source, file_type=file_type)
        self._page = 0

    # --- 页码控制 ---
    def set_page(self, page: int) -> None:
        self._page = page

    # --- 追加块 ---
    def add_heading(
        self, text: str, level: int = 1, section_no: str | None = None, page: int | None = None
    ) -> Heading:
        block = Heading(text=text, level=level, section_no=section_no, page=page or self._page)
        self._doc.blocks.append(block)
        return block

    def add_paragraph(self, text: str, style: str | None = None, page: int | None = None) -> Paragraph:
        text = text.strip()
        if not text:
            return None  # type: ignore[return-value]
        block = Paragraph(text=text, style=style, page=page or self._page)
        self._doc.blocks.append(block)
        return block

    def add_table(
        self, rows: list[list[str]], caption: str | None = None, page: int | None = None
    ) -> Table:
        block = Table(rows=rows, caption=caption, page=page or self._page)
        self._doc.blocks.append(block)
        return block

    def add_list(
        self, items: list[str], ordered: bool = False, page: int | None = None
    ) -> ListBlock:
        block = ListBlock(items=items, ordered=ordered, page=page or self._page)
        self._doc.blocks.append(block)
        return block

    def add_image(self, source: str | None = None, alt: str | None = None, page: int | None = None) -> Image:
        block = Image(source=source, alt=alt, page=page or self._page)
        self._doc.blocks.append(block)
        return block

    def add_caption(self, text: str, page: int | None = None) -> Caption:
        block = Caption(text=text, page=page or self._page)
        self._doc.blocks.append(block)
        return block

    def set_meta(self, key: str, value) -> None:
        self._doc.meta[key] = value

    def build(self) -> Document:
        return self._doc
