"""招标结构化提取结果模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Requirement(BaseModel):
    """招标要求。"""

    code: str  # R001
    category: str  # 技术要求 / 商务要求 ...
    description: str
    mandatory: bool = True
    page: int | None = None


class ScoreItem(BaseModel):
    """评分点。"""

    code: str  # S001
    item: str  # 技术方案
    score: float = 0.0  # 分值
    criteria: str = ""  # 评分标准
    page: int | None = None


class RiskItem(BaseModel):
    """风险点。"""

    code: str  # Risk001
    description: str
    level: str = "medium"  # high / medium / low
    page: int | None = None


class TenderAnalysis(BaseModel):
    """招标文件整体分析结果。"""

    requirements: list[Requirement] = Field(default_factory=list)
    score_items: list[ScoreItem] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)

    @property
    def total_score(self) -> float:
        return sum(s.score for s in self.score_items)
