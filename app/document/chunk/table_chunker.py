"""表格切分:表格保持完整,作为一个整体 Chunk,不拆散行列。"""

from __future__ import annotations

from app.core.config import settings
from app.document.ast.models import Table
from app.document.chunk.models import Chunk


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

    # 大/小表共用的字段,保证拆出来的每个 chunk 都带全,避免入库时 document_id 等为空
    common = {
        "title": caption or title,
        "document_id": document_id,
        "section_id": section_id,
        "document_type": document_type,
        "section_type": section_type,
        "page_start": table.page,
        "page_end": table.page,
        "chunk_type": "table",
    }

    # 表格尽量整体;超大表按 max_chars 拆,但以"整行"为单位
    if len(md) <= settings.chunk_max_chars:
        chunks = [
            Chunk(
                content=md,
                metadata={"kind": "table", "rows": len(table.rows)},
                **common,
            )
        ]
    else:
        chunks = _split_large_table(table.rows, common)

    # 回填索引
    for i, chunk in enumerate(chunks):
        chunk.chunk_index = start_index + i
    return chunks


def _split_large_table(rows: list[list[str]], common: dict) -> list[Chunk]:
    """超大表按行拆,每行完整。"""
    chunks: list[Chunk] = []
    if not rows:
        return chunks

    header = rows[0]
    current: list[list[str]] = [header]
    current_chars = len("|".join(header))

    for row in rows[1:]:
        row_chars = len("|".join(row))
        if current_chars + row_chars > settings.chunk_max_chars and len(current) > 1:
            chunks.append(_make_table_chunk(current, common))
            current = [header, row]
            current_chars = len("|".join(header)) + row_chars
        else:
            current.append(row)
            current_chars += row_chars

    if current:
        chunks.append(_make_table_chunk(current, common))
    return chunks


def _make_table_chunk(rows: list[list[str]], common: dict) -> Chunk:
    t = Table(rows=rows)
    return Chunk(
        content=t.to_markdown(),
        metadata={"kind": "table", "rows": len(rows)},
        **common,
    )
