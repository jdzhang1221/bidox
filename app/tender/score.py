"""评分点提取。"""

from __future__ import annotations

from app.llm.gateway import LLMGateway
from app.tender.models import ScoreItem, coerce_page, coerce_section

_SYSTEM = (
    "你是招标文件分析专家。请从给定文本中提取所有评分点(评分项),"
    "以 JSON 数组返回,每个评分点包含 code(编号S001起)、item(评分项名称)、"
    "score(分值,数字)、criteria(评分标准)、page(页码,整数,无则null)、"
    "section(该评分点所属章节,取最近的章/节标题或编号,无则null)。"
    "只输出 JSON,不要解释。"
)


class ScoreExtractor:
    def __init__(self, llm: LLMGateway | None = None) -> None:
        from app.llm.gateway import get_llm

        self._llm = llm or get_llm()

    def extract(self, text: str) -> list[ScoreItem]:
        prompt = f"招标文件文本:\n{text}"
        data = self._llm.complete_json(prompt, system=_SYSTEM)
        items = data.get("score_items", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        results: list[ScoreItem] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            it = dict(it)
            it["page"] = coerce_page(it.get("page"))
            it["section"] = coerce_section(it.get("section"))
            try:
                results.append(ScoreItem(**it))
            except Exception:
                continue
        return results
