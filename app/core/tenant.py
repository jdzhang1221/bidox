"""租户边界守卫。

检索与持久化的每个入口都必须显式带上 tenant_id：缺失、非法或与既有归属冲突时一律拒绝，
**绝不**回退成「不过滤」。这样即使上层漏传，也不会静默退化成跨租户全库检索。

`document_section` / `pattern_source` 表本身没有 tenant 列，它们的隔离靠「可信 document_id 集合」
传递，见 `app/retrieval/section.py` 的 `allowed_document_ids`。
"""

from __future__ import annotations


class TenantScopeError(ValueError):
    """租户缺失、非法或不一致。"""


def require_tenant_id(tenant_id: int | None, *, where: str) -> int:
    """校验并返回 tenant_id。

    where 为调用点标识，便于定位漏传租户的入口。
    """
    if tenant_id is None:
        raise TenantScopeError(f"{where}: 缺少 tenant_id，拒绝在无租户边界下检索或写入")
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id <= 0:
        raise TenantScopeError(f"{where}: tenant_id 非法({tenant_id!r})")
    return tenant_id


def require_allowed_document_ids(document_ids: object, *, where: str) -> set[int]:
    """把「可信 document_id 集合」规整成 set[int]；空集合返回空 set（调用方必须直接返回空结果）。

    用于没有 tenant 列的表（document_section / pattern_source）的隔离：只能在这些文档范围内扩上下文。
    """
    if document_ids is None:
        raise TenantScopeError(f"{where}: 缺少 allowed_document_ids，无法在无租户边界下扩展")
    if not isinstance(document_ids, (list, tuple, set, frozenset)):
        raise TenantScopeError(f"{where}: allowed_document_ids 类型非法({type(document_ids)!r})")
    out: set[int] = set()
    for value in document_ids:
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise TenantScopeError(f"{where}: allowed_document_ids 含非法元素({value!r})")
        out.add(value)
    return out
