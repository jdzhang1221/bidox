"""章节分类:为 Section 打 section_type 标签。

优先用规则(关键词匹配)保证可离线运行;可注入 LLM 做更精细分类。
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.document.section.models import Section

logger = get_logger(__name__)

# 企业标书章节分类(historical_bid 等)
BID_SECTION_TYPES: dict[str, list[str]] = {
    "enterprise_profile": ["公司简介", "企业概况", "企业介绍", "公司概况", "单位简介"],
    "qualification": ["资质", "资格", "证书", "荣誉", "专利", "软件著作权"],
    "project_case": ["项目案例", "业绩", "成功案例", "同类项目", "典型项目"],
    "technical_solution": ["技术方案", "总体方案", "解决方案", "建设方案"],
    "system_architecture": ["总体架构", "系统架构", "技术架构", "逻辑架构", "部署架构", "网络架构", "数据架构"],
    "implementation": ["实施方案", "实施计划", "进度计划", "项目计划", "工期", "组织方案"],
    "project_team": ["项目团队", "人员", "项目经理", "团队配置", "人员配备", "组织机构"],
    "after_sales": ["售后", "服务承诺", "运维", "质保", "维护"],
    "security": ["安全", "保密", "等保"],
    "quality": ["质量", "ISO", "管理体系"],
    "training": ["培训"],
    "maintenance": ["运维", "维护", "运行保障"],
    "commercial": ["报价", "商务", "投标函", "开标", "清单"],
}

# 招标文件章节分类(tender)
TENDER_SECTION_TYPES: dict[str, list[str]] = {
    "qualification_requirement": ["资格要求", "投标人资格", "资质要求", "资格审查"],
    "technical_requirement": ["技术要求", "技术需求", "技术参数", "功能要求", "建设内容"],
    "business_requirement": ["商务要求", "商务条件", "报价要求", "付款"],
    "score_criteria": ["评分", "评标", "评审办法", "评分标准", "分值"],
    "contract_requirement": ["合同", "协议条款"],
    "delivery_requirement": ["交付", "工期", "进度", "验收"],
    "after_sales_requirement": ["售后", "质保", "服务要求", "运维"],
}


class SectionClassifier:
    """章节分类器。"""

    def __init__(self, document_type: str = "historical_bid") -> None:
        # 根据文档类型选择分类体系
        self.rules = TENDER_SECTION_TYPES if document_type == "tender" else BID_SECTION_TYPES

    def classify(self, sections: list[Section]) -> list[Section]:
        """原地为每节打标签(递归)。"""
        for section in sections:
            self._classify_one(section)
            self.classify(section.children)
        return sections

    def _classify_one(self, section: Section) -> None:
        title = section.title or ""
        for stype, keywords in self.rules.items():
            if any(k in title for k in keywords):
                section.section_type = stype
                return
        section.section_type = "other"

    def classify_by_llm(self, section: Section, llm_client) -> str | None:
        """可选:用 LLM 分类单个章节标题。返回 section_type 或 None。"""
        if llm_client is None:
            return None
        prompt = (
            f"你是标书文档分析助手。请将章节标题归类到以下类型之一:\n"
            f"{', '.join(self.rules.keys())}\n"
            f"章节标题: {section.title}\n"
            f"只返回类型名,不要解释。"
        )
        try:
            result = llm_client.complete(prompt)
            stype = result.strip().lower()
            if stype in self.rules:
                return stype
        except Exception as exc:  # pragma: no cover - LLM 不可用时降级
            logger.warning("LLM 分类失败,回退规则: %s", exc)
        return None


# 正则预编译(备用)
_CJK_NO = re.compile(r"[一二三四五六七八九十百]+")
