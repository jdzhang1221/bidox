"""真实标书入库脚本:解析 → 双通道入库(chunk + pattern)。

用法:
    uv run python scripts/ingest_doc.py "<docx路径>" \
        --document-id 2001 --enterprise-id 1 --knowledge-base-id 1001 [--no-patterns]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.core.logging import setup_logging
from app.document.pipeline import DocumentPipeline
from app.knowledge.ingest import ingest_result


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--document-id", type=int, default=2001)
    parser.add_argument("--enterprise-id", type=int, default=1)
    parser.add_argument("--knowledge-base-id", type=int, default=1001)
    parser.add_argument("--document-type", default="historical_bid")
    parser.add_argument("--no-patterns", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"文件不存在: {args.path}")

    result = DocumentPipeline(document_id=args.document_id).run(
        path, document_type=args.document_type
    )
    print(f"[解析] section={len(result.flattened_sections)} chunk={len(result.chunks)}")

    stats = ingest_result(
        result,
        document_id=args.document_id,
        knowledge_base_id=args.knowledge_base_id,
        tenant_id=1,
        enterprise_id=args.enterprise_id,
        document_type=args.document_type,
        name=path.name,
        file_name=path.name,
        file_type=path.suffix.lstrip("."),
        file_size=path.stat().st_size,
        parser=result.document.meta.get("detected_type"),
        with_patterns=not args.no_patterns,
    )
    print(f"[入库] {stats}")


if __name__ == "__main__":
    main()
