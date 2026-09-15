"""文档类型与问答默认范围（§4.4）。

这是**叶子模块**：不 import 任何 app 内其它模块，供 models / retrieval / knowledge / pattern
各层共用，避免「models 反向依赖业务层」造成循环导入（批次 A 已在
`active_document_clause` 上踩过同一个坑）。

设计要点：

1. `tender`（招标文件）是**本次项目的约束来源**，不是可复用的历史知识。
   把它混进企业知识问答会带来两类问题：
   - 招标文件里的具体要求/评分办法被当成「企业事实」复述给下一个项目；
   - 招标文件里常含代理机构、采购人、预算等信息，属于不该跨项目复用的内容。
   因此默认 QA 类型白名单**排除 tender**。
2. pattern（方案组件）只允许来自 QA 白名单内的文档。tender 不得产生 pattern：
   Java 侧解析 tender 时 `withPatterns=false`，Python 侧在**持久化/调度层再次强制拒绝**
   （不能只靠调用方自觉）。
3. 存量数据里可能已经有 tender 来源的 pattern。这类数据不做物理删除（可能是别处共享来源），
   而是在**召回侧**通过「来源 document 类型」约束隔离，使它们永远进不了 QA 上下文。
"""

from __future__ import annotations

from typing import Iterable, Sequence

# 招标文件类型（唯一被排除的类型）
TENDER_DOCUMENT_TYPE = "tender"

# 历史遗留取值 → 当前取值。
# `tender_doc` 是早期字典里的写法，与代码统一使用的 `tender` 不一致。
# **必须映射到 tender，不能让它落到 normalize 的默认值**：那会把存量招标文件
# 静默重分类成 historical_bid，正好是 §4.4 要消除的混淆。
LEGACY_DOCUMENT_TYPE_ALIASES: dict[str, str] = {
    "tender_doc": TENDER_DOCUMENT_TYPE,
}

# 全部已知文档类型
ALL_DOCUMENT_TYPES: tuple[str, ...] = (
    "historical_bid",
    "tender",
    "enterprise_profile",
    "qualification",
    "project_case",
    "product_manual",
    "technical_document",
    "regulation",
    "other",
)

# 默认企业知识问答范围：ALL_DOCUMENT_TYPES 去掉 tender
QA_DOCUMENT_TYPES: tuple[str, ...] = tuple(
    t for t in ALL_DOCUMENT_TYPES if t != TENDER_DOCUMENT_TYPE
)

# 允许产生 pattern 的文档类型：与 QA 范围一致（tender 不产生方案组件）
PATTERN_ELIGIBLE_DOCUMENT_TYPES: tuple[str, ...] = QA_DOCUMENT_TYPES


def _canonical(document_type: str | None) -> str:
    """去空白 + 套用历史别名，得到规范取值。"""
    value = (document_type or "").strip()
    return LEGACY_DOCUMENT_TYPE_ALIASES.get(value, value)


def is_tender(document_type: str | None) -> bool:
    """是否为招标文件类型（含历史遗留写法 `tender_doc`）。"""
    return _canonical(document_type) == TENDER_DOCUMENT_TYPE


def is_pattern_eligible(document_type: str | None) -> bool:
    """该文档类型是否允许产生方案组件（pattern）。

    未知类型按「不允许」处理：宁可少抽取，也不让来历不明的类型把内容塞进方案库。
    """
    return _canonical(document_type) in PATTERN_ELIGIBLE_DOCUMENT_TYPES


def resolve_qa_document_types(document_types: Iterable[str] | None) -> list[str]:
    """把调用方传入的类型收敛成**安全的** QA 类型列表。

    规则（只收紧，不放宽）：

    - 未传 / 空 → 返回默认白名单；
    - 传了 → 与白名单取交集，且**恒定剔除 tender**（显式传 tender 也不生效）；
    - 取交集后为空 → 回退为默认白名单（而不是退化成「不过滤」）；
    - 返回顺序恒为 `ALL_DOCUMENT_TYPES` 声明顺序，保证 SQL 参数稳定可比对。
    """
    if not document_types:
        allowed = set(QA_DOCUMENT_TYPES)
    else:
        requested = {_canonical(t) for t in document_types if t}
        allowed = requested.intersection(QA_DOCUMENT_TYPES)
        if not allowed:
            allowed = set(QA_DOCUMENT_TYPES)
    return [t for t in ALL_DOCUMENT_TYPES if t in allowed]


def normalize_document_type(document_type: str | None, *, default: str = "historical_bid") -> str:
    """归一化文档类型：历史别名先套用，空白/未知一律落到 default，避免脏值进索引。"""
    value = _canonical(document_type)
    if value in ALL_DOCUMENT_TYPES:
        return value
    return default


def pattern_document_types(document_types: Sequence[str] | None = None) -> list[str]:
    """pattern 召回/回查时使用的来源文档类型集合（恒不含 tender）。"""
    return resolve_qa_document_types(document_types)
