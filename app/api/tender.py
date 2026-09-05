"""招标分析 API。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import AnalyzeRequest, ApiResponse
from app.tender.models import TenderAnalysis
from app.tender.requirement import RequirementExtractor
from app.tender.risk import RiskExtractor
from app.tender.score import ScoreExtractor

router = APIRouter(prefix="/tender", tags=["tender"])


@router.post("/analyze", response_model=ApiResponse)
def analyze_tender(req: AnalyzeRequest) -> ApiResponse:
    """招标文件结构化分析(要求/评分/风险)。"""
    analysis = TenderAnalysis()
    analysis.requirements = RequirementExtractor().extract(req.text)
    analysis.score_items = ScoreExtractor().extract(req.text)
    analysis.risks = RiskExtractor().extract(req.text)
    return ApiResponse(data=analysis.model_dump())
