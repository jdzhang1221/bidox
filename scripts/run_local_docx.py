"""本地文件端到端跑通脚本:解析 → 章节识别/分类 → 切分 → 向量化落库(pgvector)。

用法:
    uv run python scripts/run_local_docx.py "<本地文件路径>" \
        [--document-id 1] [--document-type historical_bid] \
        [--print-ddl] [--no-db]

说明:
    - 解析/章节/切分复用 DocumentPipeline,纯内存、无外部依赖。
    - 向量化默认走 hash 桩(EMBEDDING_REQUIRED=false),无需下载模型;装好
      FlagEmbedding 后自动切换真实 BGE-M3,脚本无需改动。
    - 落库复用 init_db / add_vector_extension / KnowledgeIndexer。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)


def _print_ddl() -> None:
    """打印建表语句(pgvector 扩展 + 三张表 + 索引),与 SQLAlchemy 模型一致。"""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.core.database import Base
    from app.models import (  # noqa: F401  触发模型注册到 Base.metadata
        DocumentChunk,
        DocumentRecord,
        DocumentSection,
    )

    print("-- 建表语句(由 SQLAlchemy 模型编译,可直接在 psql 执行)")
    print("CREATE EXTENSION IF NOT EXISTS vector;\n")
    for table in Base.metadata.sorted_tables:
        print(str(CreateTable(table).compile(dialect=postgresql.dialect())) + ";\n")
        for index in table.indexes:
            print(str(CreateIndex(index).compile(dialect=postgresql.dialect())) + ";\n")


def _run_pipeline(path: Path, document_id: int, document_type: str):
    from app.document.pipeline import DocumentPipeline

    pipeline = DocumentPipeline(document_id=document_id)
    return pipeline.run(path, document_type=document_type)


def _print_summary(result) -> None:
    flattened = result.flattened_sections
    table_chunks = [c for c in result.chunks if c.metadata.get("kind") == "table"]
    print(
        f"\n解析完成: block={len(result.document.blocks)} "
        f"section={len(flattened)} chunk={len(result.chunks)}(表格 {len(table_chunks)})"
    )

    print("\n-- 章节树(层级 | 类型 | 标题) --")

    def _show(sections, indent=0):
        for s in sections:
            print(f"{'  ' * indent}L{s.level} | {s.section_type or '-'} | {s.title}")
            _show(s.children, indent + 1)

    _show(result.sections)

    print(f"\n-- chunk 概览(共 {len(result.chunks)} 个) --")
    for c in result.chunks[:5]:
        text = c.content.replace("\n", " ")[:40]
        print(f"  [{c.chunk_index}] ({c.section_type or '-'}) {text}")


def _persist(result, document_id: int) -> None:
    from app.knowledge.indexer import KnowledgeIndexer

    KnowledgeIndexer().index_document(result, document_id)


def _verify(document_id: int) -> None:
    from sqlalchemy import func, select

    from app.core.database import session_scope
    from app.models.chunk import DocumentChunk
    from app.models.section import DocumentSection

    with session_scope() as session:
        sec_count = session.scalar(
            select(func.count())
            .select_from(DocumentSection)
            .where(DocumentSection.document_id == document_id)
        )
        chk_count = session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
        sections = session.execute(
            select(
                DocumentSection.id,
                DocumentSection.parent_id,
                DocumentSection.level,
                DocumentSection.section_type,
                DocumentSection.title,
            )
            .where(DocumentSection.document_id == document_id)
            .order_by(DocumentSection.sort_order)
            .limit(10)
        ).all()
        sample = session.scalar(
            select(DocumentChunk.content)
            .where(DocumentChunk.document_id == document_id)
            .limit(1)
        )

    print(f"\n-- 入库验证: document_section={sec_count} 行, document_chunk={chk_count} 行 --")
    print("-- 章节样例(id | parent_id | level | type | title) --")
    for row in sections:
        print(f"  {row.id} | {row.parent_id} | {row.level} | {row.section_type} | {row.title}")
    if sample:
        print(f"-- chunk 内容样例: {sample[:80]} --")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="本地 DOCX 端到端解析+向量化落库")
    parser.add_argument("path", help="本地文件路径")
    parser.add_argument("--document-id", type=int, default=1, help="document_id(默认 1)")
    parser.add_argument("--document-type", default="historical_bid", help="tender / historical_bid")
    parser.add_argument("--print-ddl", action="store_true", help="打印建表语句(pgvector 扩展 + 三张表)")
    parser.add_argument("--no-db", action="store_true", help="只跑解析,不落库")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"文件不存在: {args.path}")

    if args.print_ddl:
        _print_ddl()

    result = _run_pipeline(path, args.document_id, args.document_type)
    _print_summary(result)

    if args.no_db:
        print("\n(--no-db 已跳过落库)")
        return

    _persist(result, args.document_id)
    _verify(args.document_id)


if __name__ == "__main__":
    main()
