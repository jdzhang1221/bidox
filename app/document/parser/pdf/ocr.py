"""扫描 PDF OCR 解析器(基于 PaddleOCR,懒加载)。

流程:PDF -> 渲染页面图片 -> PaddleOCR -> 文字+坐标 -> 恢复阅读顺序 -> Document AST。
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger
from app.document.ast.builder import DocumentBuilder
from app.document.ast.models import Document
from app.document.parser.base import BaseParser, ParseError

logger = get_logger(__name__)


class OcrParser(BaseParser):
    """扫描件 OCR。不直接注册进默认 factory,由 pipeline 在检测到扫描件时调用。"""

    file_types = ("pdf",)

    def __init__(self) -> None:
        self._ocr = None

    def _get_ocr(self):
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise ParseError("未安装 PaddleOCR: pip install -e '.[ocr]'") from exc
            self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        return self._ocr

    def parse(self, path: str | Path) -> Document:
        try:
            import fitz  # PyMuPDF 用于渲染页面
        except ImportError as exc:
            raise ParseError("OCR 依赖 PyMuPDF: pip install pymupdf") from exc

        ocr = self._get_ocr()
        path = Path(path)
        builder = DocumentBuilder(source=str(path), file_type="pdf")
        doc = fitz.open(path)

        try:
            for page_no, page in enumerate(doc, start=1):
                builder.set_page(page_no)
                pix = page.get_pixmap(dpi=200)
                img_bytes = pix.tobytes("png")
                result = ocr.ocr(img_bytes, cls=True)
                self._feed_ocr_result(result, builder)
        finally:
            doc.close()

        builder.set_meta("ocr_engine", "paddleocr")
        return builder.build()

    @staticmethod
    def _feed_ocr_result(result, builder: DocumentBuilder) -> None:
        """把 PaddleOCR 结果转成块。按 y 坐标排序恢复阅读顺序。"""
        if not result or not result[0]:
            return
        lines = [line for line in result[0] if line]
        # 每个 line: [bbox, (text, conf)]
        lines_sorted = sorted(
            lines,
            key=lambda l: (round(l[0][0][1] / 20), l[0][0][0]),  # 按行(y)再按 x
        )
        for line in lines_sorted:
            try:
                text = line[1][0].strip()
            except (IndexError, TypeError):
                continue
            if text:
                builder.add_paragraph(text)
