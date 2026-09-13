"""方案组件(Solution Pattern)抽取。

把历史标书里的「方案型章节」抽象成可复用的结构化方案组件,
作为未来「评分点 → 方案组件 → Evidence Pack → 生成」链路的基础。
"""

from app.pattern.models import PatternRelation, SolutionPattern

__all__ = ["SolutionPattern", "PatternRelation"]
