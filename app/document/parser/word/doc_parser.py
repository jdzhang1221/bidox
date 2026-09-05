"""DOC 解析器:老式 Word 格式,经 LibreOffice 转 DOCX 后复用 docx 解析。

不自研 .doc 二进制解析。依赖系统安装 LibreOffice,`soffice` 需在 PATH 中。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.logging import get_logger
from app.document.ast.models import Document
from app.document.parser.base import BaseParser, ParseError
from app.document.parser.word.docx_parser import DocxParser

logger = get_logger(__name__)


class DocParser(BaseParser):
    file_types = ("doc",)

    def parse(self, path: str | Path) -> Document:
        path = Path(path)
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice is None:
            raise ParseError(
                "解析 .doc 需要 LibreOffice,请安装并确保 soffice 在 PATH 中"
            )

        with tempfile.TemporaryDirectory() as tmp:
            logger.info("转换 .doc -> .docx: %s", path.name)
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "docx", "--outdir", tmp, str(path)],
                capture_output=True,
                timeout=120,
            )
            if proc.returncode != 0:
                raise ParseError(f"LibreOffice 转换失败: {proc.stderr.decode(errors='ignore')}")

            converted = Path(tmp) / (path.stem + ".docx")
            if not converted.exists():
                raise ParseError("LibreOffice 转换未产出 docx 文件")

            # 复用 docx 解析
            return DocxParser().parse(converted)
