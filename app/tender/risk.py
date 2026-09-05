"""风险点提取。"""

from __future__ import annotations

from app.llm.gateway import LLMGateway
from app.tender.models import RiskItem

_SYSTEM = (
    "你是招标文件分析专家。请从给定文本中识别所有投标风险点,"
    "以 JSON 数组返回,每个风险点包含 code(编号Risk001起)、description(描述)、"
    "level(风险等级 high/medium/low)、page(页码,无则null)。"
    "只输出 JSON,不要解释。"
)


class RiskExtractor:
    def __init__(self, llm: LLMGateway | None = None) -> None:
        from app.llm.gateway import get_llm

        self._llm = llm or get_llm()

    def extract(self, text: str) -> list[RiskItem]:
        prompt = f"招标文件文本:\n{text}"
        data = self._llm.complete_json(prompt, system=_SYSTEM)
        items = data.get("risks", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return [RiskItem(**it) for it in items]
