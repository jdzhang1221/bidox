"""DOCX 解析器:python-docx 主解析 + XML 增强。

按文档顺序遍历 paragraph / table,识别标题、段落、列表、表格,输出 Document AST。
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger
from app.document.ast.builder import DocumentBuilder
from app.document.ast.models import Document
from app.document.parser.base import BaseParser, ParseError
from app.document.parser.word.xml_parser import (
    extract_section_no,
    guess_heading_level,
    parse_document_xml,
)

logger = get_logger(__name__)


class DocxParser(BaseParser):
    file_types = ("docx",)

    def parse(self, path: str | Path) -> Document:
        try:
            import docx
        except ImportError as exc:
            raise ParseError("未安装 python-docx: pip install python-docx") from exc

        path = Path(path)
        builder = DocumentBuilder(source=str(path), file_type="docx")
        xml_info = parse_document_xml(path)
        heading_map = xml_info["headings"]
        list_paras = xml_info["list_paras"]

        doc = docx.Document(str(path))
        # 文档标题(首个 Heading 1 或文件名)
        builder.set_meta("xml_enhanced", True)

        # 遍历 body 顶层元素,保持 paragraph / table 交错顺序
        body = doc.element.body
        para_idx = 0
        for child in body.iterchildren():
            tag = child.tag.split("}")[-1]
            if tag == "p":
                self._handle_paragraph(child, doc, para_idx, heading_map, list_paras, builder)
                para_idx += 1
            elif tag == "tbl":
                self._handle_table(child, doc, builder)

        return builder.build()

    # --- 内部 ---
    def _handle_paragraph(
        self, child, doc, para_idx: int, heading_map: dict, list_paras: set, builder: DocumentBuilder
    ) -> None:
        from docx.text.paragraph import Paragraph

        para = Paragraph(child, doc)
        text = para.text.strip()
        if not text:
            return

        style_name = (para.style.name if para.style is not None else "") or ""

        # 1. 标题:优先用 XML 里的样式级别,其次 python-docx 样式名,最后文本启发式
        level = heading_map.get(para_idx)
        if level is None and style_name.lower().startswith(("heading", "标题")):
            level = _style_level(style_name)

        if level is not None:
            builder.add_heading(text, level=level, section_no=extract_section_no(text))
            return

        if para_idx in list_paras:
            # 列表项:连续列表在 builder 层聚合
            builder.add_list([text], ordered=False)
            return

        # 2. 无样式时用文本启发式兜底
        guessed = guess_heading_level(text)
        if guessed is not None and len(text) <= 40:
            builder.add_heading(text, level=guessed, section_no=extract_section_no(text))
            return

        builder.add_paragraph(text, style=style_name)

    def _handle_table(self, child, doc, builder: DocumentBuilder) -> None:
        from docx.table import Table as DocxTable

        table = DocxTable(child, doc)
        rows = [[_cell_text(c) for c in row.cells] for row in table.rows]
        if rows:
            builder.add_table(rows)


def _cell_text(cell) -> str:
    return cell.text.strip()


def _style_level(style_name: str) -> int:
    """从样式名 'Heading 2' / '标题 2' 提取级别。"""
    for ch in style_name:
        if ch.isdigit():
            return int(ch)
    return 1
