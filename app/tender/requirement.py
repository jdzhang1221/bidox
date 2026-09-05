"""招标要求提取。"""

from __future__ import annotations

from app.llm.gateway import LLMGateway
from app.tender.models import Requirement

_SYSTEM = (
    "你是招标文件分析专家。请从给定文本中提取所有招标要求,"
    "以 JSON 数组返回,每个要求包含 code(编号R001起)、category(类别)、"
    "description(描述)、mandatory(是否强制,布尔)、page(页码,无则null)。"
    "只输出 JSON,不要解释。"
)


class RequirementExtractor:
    def __init__(self, llm: LLMGateway | None = None) -> None:
        from app.llm.gateway import get_llm

        self._llm = llm or get_llm()

    def extract(self, text: str) -> list[Requirement]:
        prompt = f"招标文件文本:\n{text}"
        data = self._llm.complete_json(prompt, system=_SYSTEM)
        items = data.get("requirements", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return [Requirement(**it) for it in items]
