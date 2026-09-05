"""文档解析总流水线:文件 -> AST -> Section -> Chunk。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger
from app.document.ast.models import Document
from app.document.chunk.models import Chunk
from app.document.chunk.splitter import ChunkSplitter
from app.document.parser.base import ParseError
from app.document.parser.factory import default_factory
from app.document.section.classifier import SectionClassifier
from app.document.section.detector import SectionDetector
from app.document.section.models import Section

logger = get_logger(__name__)


@dataclass
class ParseResult:
    """解析结果:AST + Section 树 + Chunk 列表。"""

    document: Document
    sections: list[Section] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def flattened_sections(self) -> list[Section]:
        """展平所有章节(用于入库)。"""
        out: list[Section] = []
        for s in self.sections:
            out.extend(s.walk())
        return out


class DocumentPipeline:
    """解析流水线。

    用法:
        pipeline = DocumentPipeline()
        result = pipeline.run("path/to/file.docx", document_type="historical_bid")
    """

    def __init__(self, document_id: int | None = None) -> None:
        self.document_id = document_id
        self._detector = SectionDetector()
        self._factory = default_factory

    def run(self, path: str | Path, document_type: str = "historical_bid") -> ParseResult:
        """执行完整解析。"""
        # 1. 解析 -> AST
        parser, detected_type = self._factory.create_for_path(path)
        document = parser.parse(path)
        document.meta["detected_type"] = detected_type

        # 2. 章节识别
        sections = self._detector.detect(document)

        # 3. 章节分类
        classifier = SectionClassifier(document_type=document_type)
        classifier.classify(sections)

        # 4. 切分
        splitter = ChunkSplitter(document_id=self.document_id, document_type=document_type)
        chunks = splitter.split(sections)

        logger.info(
            "解析完成: %s -> %d 块 / %d 章节 / %d chunks",
            Path(path).name,
            len(document.blocks),
            len(self._result_sections(sections)),
            len(chunks),
        )
        return ParseResult(document=document, sections=sections, chunks=chunks)

    @staticmethod
    def _result_sections(sections: list[Section]) -> list[Section]:
        out: list[Section] = []
        for s in sections:
            out.extend(s.walk())
        return out


__all__ = ["DocumentPipeline", "ParseResult", "ParseError"]
