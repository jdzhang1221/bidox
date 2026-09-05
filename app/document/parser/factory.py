"""解析器工厂:根据文件类型返回对应解析器。

类型检测不轻信扩展名,综合 magic number + 扩展名(见 app.core.utils)。
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger
from app.core.utils import detect_type
from app.document.parser.base import BaseParser, ParseError

logger = get_logger(__name__)


class ParserFactory:
    """维护类型 -> 解析器映射。"""

    def __init__(self) -> None:
        self._parsers: dict[str, BaseParser] = {}

    def register(self, parser: BaseParser) -> None:
        for ft in parser.file_types:
            self._parsers[ft] = parser

    def get(self, file_type: str) -> BaseParser | None:
        return self._parsers.get(file_type)

    def create_for_path(self, path: str | Path) -> tuple[BaseParser, str]:
        """根据路径返回 (parser, detected_type)。"""
        p = Path(path)
        ft = detect_type(p)
        if ft is None:
            raise ParseError(f"无法识别文件类型: {p.name}")
        parser = self.get(ft)
        if parser is None:
            raise ParseError(f"不支持的文件类型: {ft}")
        logger.info("文件 %s 识别为 %s,使用 %s", p.name, ft, type(parser).__name__)
        return parser, ft


def create_parser() -> ParserFactory:
    """创建默认 parser 工厂(注册全部解析器,懒加载重依赖)。"""
    factory = ParserFactory()

    from app.document.parser.pdf.pdf_inspector import PdfInspectorParser
    from app.document.parser.word.docx_parser import DocxParser
    from app.document.parser.word.doc_parser import DocParser

    factory.register(PdfInspectorParser())
    factory.register(DocxParser())
    factory.register(DocParser())
    return factory


# 默认单例工厂
default_factory = create_parser()
