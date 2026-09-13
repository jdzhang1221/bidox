"""Query 意图识别:规则优先(离线、零额外 LLM 延迟),把自然语言查询映射到领域与方案组件类型。

用于 Pattern 检索的 type 过滤,解决「全过程/管控机制」等通用词把不相关组件带偏的串题问题。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# domain -> (关键词, 命中的 pattern 类型)。与 app/pattern/models.py 的 PATTERN_TYPES 对齐。
_DOMAIN_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("quality", ("质量", "质检", "iso", "复核", "验收", "底稿", "质控", "质量管理"), ("quality_system", "risk_control")),
    ("schedule", ("进度", "工期", "计划", "节点", "里程碑", "排期", "时间表"), ("schedule", "resource_assurance")),
    ("organization", ("组织架构", "组织机构", "团队", "人员", "项目经理", "分工", "岗位", "人员配置"), ("org_structure", "team_config")),
    ("security", ("保密", "安全", "等保", "信息安全", "网络安全", "涉密"), ("security", "emergency")),
    ("emergency", ("应急", "预案", "突发"), ("emergency",)),
    ("after_sales", ("售后", "运维", "质保", "服务承诺", "维护", "运行保障", "响应"), ("after_sales",)),
    ("training", ("培训", "授课", "课程"), ("training",)),
    ("data_archive", ("归档", "成果整理", "数据整理", "交付物", "档案"), ("data_archive",)),
    ("service", ("服务流程", "全流程", "实施方案", "服务方案", "工作流程", "作业流程", "实施步骤"), ("service_process", "methodology")),
]

# 判定为「方案生成」任务的关键词(否则按 qa 处理)
_SOLUTION_TASK_KEYWORDS = ("如何", "编写", "撰写", "制定", "编制", "起草", "生成", "方案", "措施", "怎么写")


class QueryIntent(BaseModel):
    """查询意图。"""

    domains: list[str] = Field(default_factory=list)
    pattern_types: list[str] = Field(default_factory=list)
    task: str = "qa"  # solution_generation / qa / other

    @property
    def is_solution_generation(self) -> bool:
        return self.task == "solution_generation"


class QueryUnderstanding:
    """规则版意图识别。"""

    def classify(self, query: str) -> QueryIntent:
        q = (query or "").lower()
        domains: list[str] = []
        types: list[str] = []
        for domain, keywords, ptypes in _DOMAIN_RULES:
            if any(k in q for k in keywords):
                domains.append(domain)
                for t in ptypes:
                    if t not in types:
                        types.append(t)
        task = "solution_generation" if any(k in q for k in _SOLUTION_TASK_KEYWORDS) else "qa"
        return QueryIntent(domains=domains, pattern_types=types, task=task)
