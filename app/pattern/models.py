"""方案组件(Solution Pattern)模型。

区别于 chunk:chunk 是「知识片段」,Solution Pattern 是「可整体复用的方案单元」,
保存完整上下文(章节全文) + 结构化描述(summary/structure) + 跨组件关系。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 方案组件关系类型
REL_TYPES = ("related", "prerequisite", "complementary", "derived_from")

# 参考类型词表(仅作 LLM 提示,不硬约束)
PATTERN_TYPES = (
    "service_process",  # 全流程/服务流程
    "org_structure",  # 组织架构
    "team_config",  # 人员配置/团队
    "quality_system",  # 质量保障/复核体系
    "schedule",  # 进度/工期保障
    "resource_assurance",  # 资源保障
    "risk_control",  # 风险防范/重难点应对
    "emergency",  # 应急方案
    "security",  # 保密/安全
    "after_sales",  # 售后/运维
    "data_archive",  # 成果归档/数据整理
    "training",  # 培训
    "methodology",  # 方法论/技术方案
)


class SolutionPattern(BaseModel):
    """一个可复用的方案组件。"""

    code: str  # SP001(脚本顺序赋值)
    name: str  # 通用化组件名(去掉本项目具体数字)
    type: str = "methodology"  # 组件类型,见 PATTERN_TYPES
    industry: str = ""  # 所属行业,如"审计服务"
    scenario: str = ""  # 适用场景,如"政府/工会审计"

    summary: str = ""  # 通用化摘要(保留方法,去掉具体数字)
    structure: list[str] = Field(default_factory=list)  # 子模块列表

    content: str = ""  # 章节完整正文(程序填充,非 LLM 产出,保证无损)
    generalized_content: str = ""  # 去事实化正文(LLM 生成,用于方案复用)
    source: dict[str, Any] = Field(default_factory=dict)  # 溯源:文档/章节/页码

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class PatternRelation(BaseModel):
    """方案组件之间的关系。"""

    source: str  # SP 码,如 "SP001"
    target: str  # SP 码,如 "SP005"
    rel_type: str = "related"  # related / prerequisite / complementary / derived_from
