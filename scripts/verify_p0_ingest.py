"""P0 端到端验证:合成 docx → 解析 → 一次入库(document+section+chunk+可选 pattern) → DB 校验。

用法:
    uv run python scripts/verify_p0_ingest.py [--document-id 9001] [--with-patterns]

说明:
    - 用 python-docx 生成一份带 Heading 层级 + 段落 + 表格的合成标书。
    - 走 ingest_result,验证 document / document_section / document_chunk /
      solution_pattern / pattern_source 六表全链路关联。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from docx import Document as Docx

from app.core.logging import setup_logging

_SECTIONS = [
    ("第一章 服务方案", 1, None),
    ("1.1 全过程服务方案", 2, "服务流程"),
    ("1.2 质量管理方案", 2, "质量保障"),
    ("1.3 进度保障方案", 2, "进度安排"),
    ("第二章 组织与人员", 1, None),
    ("2.1 项目组织架构", 2, "组织架构"),
    ("2.2 人员配置与分工", 2, "人员配置"),
]


def _para_text(topic: str, n: int) -> str:
    return (
        f"针对{topic}，本项目将建立覆盖事前、事中、事后的全过程管控机制，"
        f"明确责任分工与关键控制节点，配置专职管理人员，制定标准化作业流程，"
        f"并通过定期检查、整改与复盘确保各项措施落地，形成可追溯的管理闭环，"
        f"持续提升服务质量与客户满意度，满足招标文件全部实质性要求。"
        f"第{n}段补充说明具体执行步骤与量化考核指标。"
    )


def _build_docx(path: Path) -> None:
    doc = Docx()
    doc.add_heading("XX 项目投标文件", level=0)
    for title, level, _ in _SECTIONS:
        doc.add_heading(title, level=level)
        if level == 2:
            for i in range(1, 5):
                doc.add_paragraph(_para_text(title, i))
            if title.startswith("1.1"):
                table = doc.add_table(rows=3, cols=3)
                table.style = "Light Grid Accent 1"
                for r, row in enumerate([["阶段", "控制节点", "责任人"], ["准备", "需求确认", "项目经理"], ["交付", "成果验收", "质量负责人"]]):
                    for c, val in enumerate(row):
                        table.rows[r].cells[c].text = val
    doc.save(path)


def _run(path: Path, document_id: int, with_patterns: bool) -> None:
    from app.document.pipeline import DocumentPipeline
    from app.knowledge.ingest import ingest_result

    result = DocumentPipeline(document_id=document_id).run(path, document_type="historical_bid")
    print(f"\n[解析] section={len(result.flattened_sections)} chunk={len(result.chunks)}")

    stats = ingest_result(
        result,
        document_id=document_id,
        knowledge_base_id=1001,
        tenant_id=1,
        enterprise_id=1,
        document_type="historical_bid",
        name=path.name,
        file_name=path.name,
        file_type="docx",
        parser="docx",
        with_patterns=with_patterns,
    )
    print(f"[入库] {stats}")


def _verify(document_id: int) -> None:
    from sqlalchemy import func, select

    from app.core.database import session_scope
    from app.models.chunk import DocumentChunk
    from app.models.document import DocumentRecord
    from app.models.pattern import PatternSource, SolutionPattern
    from app.models.section import DocumentSection

    with session_scope() as session:
        doc = session.get(DocumentRecord, document_id)
        sec_cnt = session.scalar(
            select(func.count()).select_from(DocumentSection).where(DocumentSection.document_id == document_id)
        )
        chk_cnt = session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        # 校验 chunk 的 section_id 已回填(非空)且 knowledge_base_id 冗余到位
        chk_linked = session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id, DocumentChunk.section_id.is_not(None))
        )
        chk_kb = session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id, DocumentChunk.knowledge_base_id == 1001)
        )
        # chunk_type 分布
        type_rows = session.execute(
            select(DocumentChunk.chunk_type, func.count())
            .where(DocumentChunk.document_id == document_id)
            .group_by(DocumentChunk.chunk_type)
        ).all()
        # pattern 侧
        pat_cnt = session.scalar(
            select(func.count()).select_from(SolutionPattern).where(SolutionPattern.knowledge_base_id == 1001)
        )
        src_cnt = session.scalar(
            select(func.count()).select_from(PatternSource).where(PatternSource.document_id == document_id)
        )
        src_linked = session.scalar(
            select(func.count())
            .select_from(PatternSource)
            .where(PatternSource.document_id == document_id, PatternSource.section_id.is_not(None))
        )

    print("\n===== DB 校验 =====")
    print(f"document: id={doc.id if doc else None} name={doc.name if doc else None} kb={doc.knowledge_base_id if doc else None}")
    print(f"document_section: {sec_cnt} 行")
    print(f"document_chunk: {chk_cnt} 行 (section_id 回填 {chk_linked}, kb 冗余 {chk_kb})")
    print(f"chunk_type 分布: {[(t, c) for t, c in type_rows]}")
    print(f"solution_pattern: {pat_cnt} 行 (kb=1001)")
    print(f"pattern_source: {src_cnt} 行 (section_id 关联 {src_linked})")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--document-id", type=int, default=9001)
    parser.add_argument("--with-patterns", action="store_true")
    args = parser.parse_args()

    path = Path("data/_verify_p0.docx")
    _build_docx(path)
    _run(path, args.document_id, args.with_patterns)
    _verify(args.document_id)


if __name__ == "__main__":
    main()
