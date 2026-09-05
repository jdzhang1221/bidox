"""pdf-inspector:电子 PDF 主解析器(基于 PyMuPDF)。

输出保留页码/文本/块/字体/标题/表格/阅读顺序的 Document AST。
若检测到文本层为空(扫描件),则交由 OCR 处理;复杂排版交由 MinerU。
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger
from app.document.ast.builder import DocumentBuilder
from app.document.ast.models import Document
from app.document.parser.base import BaseParser, ParseError

logger = get_logger(__name__)


class PdfInspectorParser(BaseParser):
    file_types = ("pdf",)

    def __init__(self, text_ratio_threshold: float = 0.02) -> None:
        # 有效文本占比低于该阈值判定为扫描件
        self.text_ratio_threshold = text_ratio_threshold

    def parse(self, path: str | Path) -> Document:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise ParseError("未安装 PyMuPDF: pip install pymupdf") from exc

        path = Path(path)
        builder = DocumentBuilder(source=str(path), file_type="pdf")
        doc = fitz.open(path)
        total_chars = 0

        try:
            for page_no, page in enumerate(doc, start=1):
                builder.set_page(page_no)
                text = page.get_text("text")
                total_chars += len(text.strip())
                self._parse_page(page, builder, page_no)
        finally:
            doc.close()

        document = builder.build()
        builder.set_meta("is_scanned", self._is_scanned(total_chars, len(doc)))
        document.meta = builder.build().meta

        if document.meta.get("is_scanned"):
            logger.warning("PDF 文本层为空或过少,判定为扫描件,应转 OCR: %s", path.name)
        return document

    # --- 内部 ---
    def _parse_page(self, page, builder: DocumentBuilder, page_no: int) -> None:
        """解析单页:优先用 dict 结构保留阅读顺序与块类型。"""
        try:
            page_dict = page.get_text("dict")
        except Exception:
            # 降级:纯文本
            for line in page.get_text("text").splitlines():
                if line.strip():
                    builder.add_paragraph(line)
            return

        for block in page_dict.get("blocks", []):
            btype = block.get("type")
            if btype == 0:  # 文本块
                self._parse_text_block(block, builder)
            elif btype == 1:  # 图片块
                builder.add_image(alt="pdf_image")

    def _parse_text_block(self, block: dict, builder: DocumentBuilder) -> None:
        """解析文本块,按字号识别标题,按行拼接段落。"""
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            max_size = max((s.get("size", 0) for s in spans), default=0)
            bold = any(bool(s.get("flags", 0) & 2**4) for s in spans)

            # 标题启发式:字号显著大于正文且文本较短
            if max_size >= 16 and len(text) <= 60:
                builder.add_heading(text, level=1)
            elif bold and len(text) <= 40:
                builder.add_heading(text, level=2)
            else:
                builder.add_paragraph(text)

    def _is_scanned(self, total_chars: int, page_count: int) -> bool:
        if page_count == 0:
            return True
        return (total_chars / page_count) < self.text_ratio_threshold * 1000
