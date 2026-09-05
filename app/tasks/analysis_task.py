"""招标结构化分析任务:要求 / 评分 / 风险提取。"""

from __future__ import annotations

from app.core.logging import get_logger
from app.tasks.schemas import AnalysisTaskMessage
from app.tender.models import TenderAnalysis
from app.tender.requirement import RequirementExtractor
from app.tender.risk import RiskExtractor
from app.tender.score import ScoreExtractor

logger = get_logger(__name__)


def handle_analysis_task(message: AnalysisTaskMessage, text: str) -> TenderAnalysis:
    """处理招标分析任务,输出结构化结果。"""
    logger.info("处理分析任务: task=%s document=%s", message.task_id, message.document_id)

    analysis = TenderAnalysis()
    analysis.requirements = RequirementExtractor().extract(text)
    analysis.score_items = ScoreExtractor().extract(text)
    analysis.risks = RiskExtractor().extract(text)

    logger.info(
        "分析完成: 要求 %d / 评分 %d / 风险 %d",
        len(analysis.requirements),
        len(analysis.score_items),
        len(analysis.risks),
    )
    return analysis
