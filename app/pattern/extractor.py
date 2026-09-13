"""方案组件抽取:把「方案型章节」抽象成 Solution Pattern。

边界策略(以章节为边界):复用 Section 树,一个满足条件的章节 = 一个方案组件。
LLM 只负责通用化总结与结构化(填 name/type/industry/scenario/summary/structure),
正文 content 由代码从 section.full_content 无损填充。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import get_logger
from app.document.section.models import Section
from app.llm.gateway import LLMGateway
from app.pattern.models import PATTERN_TYPES, REL_TYPES, PatternRelation, SolutionPattern

logger = get_logger(__name__)

_SYSTEM = (
    "你是标书方案分析专家。给定一个历史标书章节(含标题与正文),"
    "请把它抽象成一个可复用的「方案组件(Solution Pattern)」。\n"
    "要求:\n"
    "1. 通用化:去掉本项目特有数字(如「11家/90日历天/25个节点/8名人员」),保留通用方法。\n"
    f"2. type 从下列类型选最贴近的一个,必要时可自定义: {', '.join(PATTERN_TYPES)}。\n"
    "3. structure 按原文结构顺序列出该方案的核心子模块。\n"
    "4. industry/scenario 推断所属行业与适用场景。\n"
    "5. generalized_content 输出该章节的「去事实化正文」:保留完整方法与行文结构,但把具体数字、单位、"
    "人名、机构名、项目名、日期等一律替换为通用表述(如「11家直属基层工会」→「被审计单位」,"
    "「90日历天」→「合同约定期限内」),篇幅与原文相当。\n"
    "只输出 JSON 对象,格式:\n"
    '{"name":"...","type":"...","industry":"...","scenario":"...","summary":"...","structure":["..."],"generalized_content":"..."}'
)

_REL_SYSTEM = (
    "你是标书方案分析专家。下面是已抽取的方案组件列表(code/name/summary)。"
    "请识别组件之间的关联关系,只输出 JSON 数组,每个元素为 "
    '{"source":"SP001","target":"SP005","rel_type":"related"},'
    "rel_type 取值 related / prerequisite / complementary / derived_from。"
)

# 非方案章节:投标行政表单 / 资格证明 / 业绩,不是可复用的方案单元。
NON_SOLUTION_SECTION_TYPES = {"qualification", "project_case", "commercial"}
NON_SOLUTION_TITLE_KEYWORDS = (
    "响应函", "投标函", "授权书", "承诺函", "偏差表",
    "简历", "一览表", "证明材料", "综合说明", "报价", "响应与偏差",
)

# type 别名归一化:把 LLM 自由输出映射到受控枚举 PATTERN_TYPES。
_TYPE_ALIASES: dict[str, str] = {
    "resource": "resource_assurance",
    "resource_assurance": "resource_assurance",
    "schedule_management": "schedule",
    "progress": "schedule",
    "process": "service_process",
    "service_flow": "service_process",
    "quality": "quality_system",
    "risk": "risk_control",
    "team": "team_config",
    "organization": "org_structure",
    "org": "org_structure",
    "training_system": "training",
    "archive": "data_archive",
    "confidentiality": "security",
    "after_service": "after_sales",
}


def _normalize_type(raw: str) -> str:
    """把 LLM 自由输出的 type 归一化到受控枚举 PATTERN_TYPES。"""
    key = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in PATTERN_TYPES:
        return key
    return _TYPE_ALIASES.get(key, "methodology")


def _is_non_solution(section: Section) -> bool:
    """判断章节是否为投标行政/资格/业绩文书,而非可复用方案单元。"""
    if (section.section_type or "") in NON_SOLUTION_SECTION_TYPES:
        return True
    title = section.title or ""
    return any(k in title for k in NON_SOLUTION_TITLE_KEYWORDS)


class SolutionPatternExtractor:
    """方案组件抽取器(LLM 结构化抽取)。"""

    def __init__(self, llm: LLMGateway | None = None) -> None:
        from app.llm.gateway import get_llm

        self._llm = llm or get_llm()

    # --- 候选章节选择 ---
    @staticmethod
    def select_candidates(
        sections: list[Section],
        min_level: int = 2,
        max_level: int = 3,
        min_chars: int = 300,
        filter_non_solution: bool = True,
    ) -> list[Section]:
        """按层级 + 全文长度筛选方案型章节,可选剔除投标行政/资格/业绩章节。"""
        flat: list[Section] = []
        for s in sections:
            flat.extend(s.walk())
        out: list[Section] = []
        for s in flat:
            if not (min_level <= s.level <= max_level):
                continue
            if len(s.full_content.strip()) < min_chars:
                continue
            if filter_non_solution and _is_non_solution(s):
                continue
            out.append(s)
        return out

    # --- 单章节抽取 ---
    def extract(
        self,
        section: Section,
        code: str,
        doc_meta: dict[str, Any] | None = None,
        max_prompt_chars: int = 6000,
        timeout: float | None = None,
    ) -> SolutionPattern:
        """把一个章节抽象为 SolutionPattern。"""
        doc_meta = doc_meta or {}
        full_text = section.full_content.strip()
        prompt = (
            f"章节标题: {section.title}\n"
            f"章节编号: {section.section_no or ''}\n"
            f"正文:\n{full_text[:max_prompt_chars]}"
        )
        data = self._llm.complete_json(prompt, system=_SYSTEM, timeout=timeout)
        if not isinstance(data, dict):
            raise ValueError(f"方案组件抽取返回非对象: {type(data)}")

        return SolutionPattern(
            code=code,
            name=str(data.get("name", "")).strip() or section.title,
            type=_normalize_type(data.get("type", "")),
            industry=str(data.get("industry", "")).strip(),
            scenario=str(data.get("scenario", "")).strip(),
            summary=str(data.get("summary", "")).strip(),
            structure=_as_str_list(data.get("structure")),
            content=full_text,
            generalized_content=str(data.get("generalized_content", "")).strip(),
            source={
                "document_title": doc_meta.get("title"),
                "section_no": section.section_no,
                "section_title": section.title,
                "section_type": section.section_type,
                "page_start": section.page_start,
                "page_end": section.page_end,
            },
        )

    # --- 关系抽取 ---
    def extract_relations(
        self,
        patterns: list[SolutionPattern],
        batch_size: int = 8,
        timeout: float | None = 300.0,
    ) -> list[PatternRelation]:
        """抽取组件间关系。

        先尝试一次全量调用(关系质量最好:可跨全部组件配对);若失败(超时/格式错误),
        回退为分批抽取,单批失败只丢该批。任何情况下都不抛异常打断主流程——
        关系是增强信息,不该让已抽取的组件白跑。
        """
        if len(patterns) < 2:
            return []

        full = self._relations_for(patterns, timeout=timeout)
        if full is not None:
            return full

        logger.warning("全量关系抽取失败,回退分批抽取(batch_size=%d,跨批关系会丢失)", batch_size)
        merged: list[PatternRelation] = []
        seen: set[tuple[str, str]] = set()
        for i in range(0, len(patterns), batch_size):
            batch = patterns[i : i + batch_size]
            if len(batch) < 2:
                continue
            for rel in self._relations_for(batch, timeout=timeout) or []:
                key = (rel.source, rel.target)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(rel)
        return merged

    def _relations_for(
        self, group: list[SolutionPattern], timeout: float | None = None
    ) -> list[PatternRelation] | None:
        """对一组组件调用一次 LLM 抽关系;失败返回 None(交由上层降级)。"""
        items = [{"code": p.code, "name": p.name, "summary": p.summary[:120]} for p in group]
        prompt = f"组件列表:\n{json.dumps(items, ensure_ascii=False)}"
        try:
            data = self._llm.complete_json(prompt, system=_REL_SYSTEM, timeout=timeout)
        except Exception as exc:  # 超时/限流/JSON 非法:降级而非中断
            logger.warning("关系抽取失败(%d 个组件): %s", len(group), exc)
            return None

        rows = data.get("relations", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            logger.warning("关系抽取返回非数组: %s", type(rows))
            return []

        relations: list[PatternRelation] = []
        valid_codes = {p.code for p in group}
        for row in rows:
            if not isinstance(row, dict):
                continue
            source, target, rel = (
                row.get("source"),
                row.get("target"),
                row.get("rel_type", "related"),
            )
            if source not in valid_codes or target not in valid_codes or source == target:
                continue
            relations.append(
                PatternRelation(
                    source=str(source),
                    target=str(target),
                    rel_type=str(rel) if str(rel) in REL_TYPES else "related",
                )
            )
        return relations


def _as_str_list(value: Any) -> list[str]:
    """把 LLM 返回的 structure 归一化为 list[str]。"""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


__all__ = ["SolutionPatternExtractor"]
