"""MinerU 增强解析器(懒加载)。

定位:复杂 PDF(复杂表格/多栏/图文混排/公式)的增强解析,不替代主链路。
由 pipeline 在结构质量不足时按需调用。
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger
from app.document.ast.models import Document
from app.document.parser.base import BaseParser, ParseError

logger = get_logger(__name__)


class MineruParser(BaseParser):
    """MinerU 解析。输出结构增强结果(Markdown/JSON),再由 adapter 转 AST。"""

    file_types = ("pdf",)

    def parse(self, path: str | Path) -> Document:
        try:
            from magic_pdf.pipe.UNIPipe import UNIPipe
            from magic_pdf.rw.DiskReaderWriter import DiskReaderWriter
        except ImportError as exc:
            raise ParseError("未安装 MinerU: pip install -e '.[mineru]'") from exc

        # MinerU 需要指定的模型目录与设备,这里保留骨架:
        # 实际接入时:
        #   1. 下载模型 json/pdf_parse/model
        #   2. 用 UNIPipe 解析 -> 得到 markdown 内容
        #   3. 转成 Document AST
        raise NotImplementedError(
            "MinerU 接入骨架已就位,需补充模型目录配置与 markdown->AST 转换"
        )


def mineru_available() -> bool:
    """检测 MinerU 是否可用。"""
    try:
        import magic_pdf  # noqa: F401

        return True
    except ImportError:
        return False
