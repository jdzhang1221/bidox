"""文档解析器:pdf / word 解析 + factory。"""

from app.document.parser.base import BaseParser, ParseError
from app.document.parser.factory import ParserFactory, create_parser

__all__ = ["BaseParser", "ParseError", "ParserFactory", "create_parser"]
