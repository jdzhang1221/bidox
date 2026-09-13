"""Chunk 主切分器:标题层级 + 语义 + 长度 + 表格完整性综合切分。

策略:
  - 以 Section 为单位组织内容
  - 表格整体成一个 Chunk
  - 文本按语义切分,标题作为 chunk 标题
  - 保留 page / section 信息
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.document.ast.models import Caption, Heading, ListBlock, Paragraph, Table
from app.document.chunk.models import Chunk
from app.document.chunk.semantic_chunker import semantic_split
from app.document.chunk.table_chunker import table_to_chunks
from app.document.section.models import Section

logger = get_logger(__name__)


class ChunkSplitter:
    """把 Section 树切分成 Chunk 列表。"""

    def __init__(self, document_id: int | None = None, document_type: str | None = None) -> None:
        self.document_id = document_id
        self.document_type = document_type
        self._index = 0

    def split(self, sections: list[Section]) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in sections:
            chunks.extend(self._split_section(section))
        return chunks

    # --- 内部 ---
    def _split_section(self, section: Section) -> list[Chunk]:
        chunks: list[Chunk] = []
        title = section.title or None
        section_type = section.section_type

        # 1. 该章节直属内容:把连续段落/列表/表格组织起来
        text_buffer: list[str] = []
        current_page_start = section.page_start

        for block in section.content_blocks:
            if isinstance(block, Table):
                # 先落文本缓冲
                self._flush_text(text_buffer, title, section_type, section, chunks)
                text_buffer = []
                # 表格整体成 chunk
                table_chunks = table_to_chunks(
                    block,
                    title=title,
                    document_id=self.document_id,
                    section_id=section.id,  # 临时 id,入库后映射为真实 section_id
                    document_type=self.document_type,
                    section_type=section_type,
                    start_index=self._index,
                )
                self._index += len(table_chunks)
                chunks.extend(table_chunks)
            elif isinstance(block, (Paragraph, Heading, Caption)):
                text_buffer.append(block.text)
            elif isinstance(block, ListBlock):
                text_buffer.append(block.to_text())

        self._flush_text(text_buffer, title, section_type, section, chunks)

        # 2. 递归子章节
        for child in section.children:
            chunks.extend(self._split_section(child))
        return chunks

    def _flush_text(
        self,
        text_buffer: list[str],
        title: str | None,
        section_type: str | None,
        section: Section,
        chunks: list[Chunk],
    ) -> None:
        if not text_buffer:
            return
        raw = "\n\n".join(t for t in text_buffer if t)
        for piece in semantic_split(raw):
            chunks.append(
                Chunk(
                    content=piece,
                    title=title,
                    document_id=self.document_id,
                    section_id=section.id,  # 临时 id,入库后映射为真实 section_id
                    document_type=self.document_type,
                    section_type=section_type,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    chunk_index=self._index,
                )
            )
            self._index += 1
        text_buffer.clear()
