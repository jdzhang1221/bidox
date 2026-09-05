"""表格切分:表格保持完整,作为一个整体 Chunk,不拆散行列。"""

from __future__ import annotations

from app.core.config import settings
from app.document.ast.models import Table
from app.document.chunk.models import Chunk
from app.document.chunk.semantic_chunker import semantic_split


def table_to_chunks(
    table: Table,
    title: str | None = None,
    document_id: int | None = None,
    section_id: int | None = None,
    document_type: str | None = None,
    section_type: str | None = None,
    start_index: int = 0,
) -> list[Chunk]:
    """表格 -> 完整 Chunk(必要时拆多行,但保证每行完整)。"""
    md = table.to_markdown()
    caption = table.caption or title

    # 表格尽量整体;超大表按 max_chars 拆,但以"整行"为单位
    if len(md) <= settings.chunk_max_chars:
        content = md
        chunks = [Chunk(
            content=content,
            title=caption or title,
            document_id=document_id,
            section_id=section_id,
            document_type=document_type,
            section_type=section_type,
            page_start=table.page,
            page_end=table.page,
            metadata={"kind": "table", "rows": len(table.rows)},
        )]
    else:
        chunks = _split_large_table(md, table.rows, title, caption)

    # 回填索引
    for i, chunk in enumerate(chunks):
        chunk.chunk_index = start_index + i
    return chunks


def _split_large_table(
    md: str, rows: list[list[str]], title: str | None, caption: str | None
) -> list[Chunk]:
    """超大表按行拆,每行完整。"""
    from app.document.chunk.models import Chunk

    chunks: list[Chunk] = []
    header = rows[0] if rows else []
    current: list[list[str]] = list(header)
    current_chars = len("|".join(header))

    for row in rows[1:]:
        row_chars = len("|".join(row))
        if current_chars + row_chars > settings.chunk_max_chars and len(current) > len(header):
            chunks.append(_make_table_chunk(current, title, caption))
            current = list(header) + [row]
            current_chars = len("|".join(header)) + row_chars
        else:
            current.append(row)
            current_chars += row_chars
    if current:
        chunks.append(_make_table_chunk(current, title, caption))
    return chunks


def _make_table_chunk(rows: list[list[str]], title: str | None, caption: str | None) -> Chunk:
    from app.document.chunk.models import Chunk

    t = Table(rows=rows)
    return Chunk(
        content=t.to_markdown(),
        title=caption or title,
        metadata={"kind": "table", "rows": len(rows)},
    )
