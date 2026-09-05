"""解析器基类。"""

from __future__ import annotations

from pathlib import Path

from app.document.ast.models import Document


class ParseError(Exception):
    """解析失败。"""


class BaseParser:
    """文档解析器接口:输入文件路径,输出 Document AST。"""

    #: 支持的文件类型
    file_types: tuple[str, ...] = ()

    def parse(self, path: str | Path) -> Document:
        raise NotImplementedError

    def supports(self, file_type: str) -> bool:
        return file_type in self.file_types
