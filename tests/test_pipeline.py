"""解析流水线冒烟测试:用内存生成 docx,验证 AST -> Section -> Chunk。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_docx(path: Path) -> None:
    """用 python-docx 生成一个带标题/段落/表格的样例文档。"""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = docx.Document()

    doc.add_heading("第一章 项目概述", level=1)
    doc.add_heading("1.1 项目背景", level=2)
    doc.add_paragraph("随着智慧城市建设推进,需要对现有平台进行升级改造。")

    doc.add_heading("第二章 技术方案", level=1)
    doc.add_heading("2.1 总体架构", level=2)
    doc.add_paragraph("本系统采用微服务架构,前后端分离。")

    # 表格(评分表)
    table = doc.add_table(rows=3, cols=3)
    headers = ["评分项", "分值", "评分标准"]
    rows = [
        ["技术方案", "30", "方案完整、合理、先进"],
        ["项目团队", "20", "人员配置合理"],
    ]
    for j, h in enumerate(headers):
        table.rows[0].cells[j].text = h
    for i, row in enumerate(rows, start=1):
        for j, val in enumerate(row):
            table.rows[i].cells[j].text = val

    doc.save(str(path))


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("docx"),
    reason="python-docx 未安装",
)
def test_docx_pipeline(tmp_path: Path) -> None:
    from app.document.pipeline import DocumentPipeline

    path = tmp_path / "sample.docx"
    _make_docx(path)

    result = DocumentPipeline(document_id=1).run(path, document_type="historical_bid")

    # AST 有块
    assert len(result.document.blocks) > 0
    # 有标题
    assert any(b.type == "heading" for b in result.document.blocks)
    # 有章节树
    assert len(result.sections) >= 2
    # 有 chunk
    assert len(result.chunks) > 0
    # 表格作为完整 chunk(含评分标准)
    table_chunks = [c for c in result.chunks if c.metadata.get("kind") == "table"]
    assert table_chunks, "表格应作为完整 chunk"
    assert any("评分标准" in c.content for c in table_chunks)


def test_semantic_split() -> None:
    from app.document.chunk.semantic_chunker import semantic_split

    text = "第一段。" + "内容" * 300
    pieces = semantic_split(text, max_chars=500)
    assert len(pieces) >= 1
    assert all(len(p) <= 500 + 200 for p in pieces)  # 留 overlap 余量
