"""招标要求提取。"""

from __future__ import annotations

from app.llm.gateway import LLMGateway
from app.tender.models import Requirement, coerce_page, coerce_section

_SYSTEM = (
    "你是招标文件分析专家。请从给定文本中提取所有招标要求(实质性要求),"
    "以 JSON 数组返回,每个要求包含 code(编号R001起)、category(类别)、"
    "description(描述)、mandatory(是否强制,布尔)、page(页码,整数,无则null)、"
    "section(该要求所属章节,取最近的章/节标题或编号,如'第三章 采购需求'、'5.1.2',无则null)。"
    "注意:评分标准/评审办法里的打分项(如'最高7分''每项2分''满分'等)不属于招标要求,不要提取,"
    "它们由评分点单独处理;只提取资格要求、符合性/资格性审查、采购范围、服务要求、商务要求、"
    "响应文件要求、报价要求、履约要求等实质性要求。"
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
        requirements: list[Requirement] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            it = dict(it)
            it["page"] = coerce_page(it.get("page"))
            it["section"] = coerce_section(it.get("section"))
            try:
                req = Requirement(**it)
            except Exception:
                continue
            if "评分" in (req.category or ""):
                continue  # 评分标准/打分项归 ScoreExtractor,不混入要求
            requirements.append(req)
        return requirements
