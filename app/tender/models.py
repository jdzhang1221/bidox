"""招标结构化提取结果模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def coerce_page(value: Any) -> int | None:
    """LLM 页码容错:整数直取,数字字符串取首个数字,其余返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None
    return None


def coerce_section(value: Any) -> str | None:
    """LLM 章节容错:非空字符串直取,其余返回 None。"""
    return value if isinstance(value, str) and value.strip() else None


class Requirement(BaseModel):
    """招标要求。"""

    code: str  # R001
    category: str  # 技术要求 / 商务要求 ...
    description: str
    mandatory: bool = True
    page: int | None = None
    section: str | None = None  # 所属章节(章/节标题或编号)


class ScoreItem(BaseModel):
    """评分点。"""

    code: str  # S001
    item: str  # 技术方案
    score: float = 0.0  # 分值
    criteria: str = ""  # 评分标准
    page: int | None = None
    section: str | None = None  # 所属章节(章/节标题或编号)


class RiskItem(BaseModel):
    """风险点。"""

    code: str  # Risk001
    description: str
    level: str = "medium"  # high / medium / low
    page: int | None = None
    section: str | None = None  # 所属章节(章/节标题或编号)


class TenderAnalysis(BaseModel):
    """招标文件整体分析结果。"""

    requirements: list[Requirement] = Field(default_factory=list)
    score_items: list[ScoreItem] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)

    @property
    def total_score(self) -> float:
        return sum(s.score for s in self.score_items)
