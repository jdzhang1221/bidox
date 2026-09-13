"""招标分析 API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.api.schemas import AnalyzeRequest, ApiResponse, TenderAnalyzeLocalRequest
from app.document.pipeline import DocumentPipeline
from app.tender.models import TenderAnalysis
from app.tender.requirement import RequirementExtractor
from app.tender.risk import RiskExtractor
from app.tender.score import ScoreExtractor

router = APIRouter(prefix="/tender", tags=["tender"])


def _analyze_text(text: str) -> TenderAnalysis:
    """对招标文件文本做三段结构化提取(要求/评分/风险)。"""
    analysis = TenderAnalysis()
    analysis.requirements = RequirementExtractor().extract(text)
    analysis.score_items = ScoreExtractor().extract(text)
    analysis.risks = RiskExtractor().extract(text)
    return analysis


@router.post("/analyze", response_model=ApiResponse)
def analyze_tender(req: AnalyzeRequest) -> ApiResponse:
    """招标文件结构化分析(要求/评分/风险)。"""
    analysis = _analyze_text(req.text)
    return ApiResponse(data=analysis.model_dump())


@router.post("/analyze-local", response_model=ApiResponse)
def analyze_tender_local(req: TenderAnalyzeLocalRequest) -> ApiResponse:
    """本地招标文件分析(直接传路径,内部解析→提取要求/评分/风险)。"""
    path = Path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"本地文件不存在: {req.path}")

    result = DocumentPipeline().run(path, document_type=req.document_type)

    # 拼全文:段落/标题/题注取 text,表格转 Markdown,列表转文本,自动跳过图片;
    # 页码变化处插入 [第N页] 标记(Word 无页码时退化为纯文本)
    text = result.document.to_text_with_pages()

    analysis = _analyze_text(text)
    data = analysis.model_dump()
    data["stats"] = {
        "block_count": len(result.document.blocks),
        "section_count": len(result.flattened_sections),
        "chunk_count": len(result.chunks),
        "text_chars": len(text),
    }
    return ApiResponse(data=data)
