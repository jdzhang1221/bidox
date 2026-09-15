"""索引生命周期守卫的单元测试（批次 A / §4.3）。

这里只测**纯函数**与**请求契约**，不连数据库：真实 PG 上的行锁、partial index、UUID、
事务回滚、并发 ingest/delete、V1/V2 pattern 竞争由 `scripts/verify_index_lifecycle.py` 覆盖。
两者互补——本文件保证判定矩阵不被改坏，脚本保证真库行为正确。
"""

from __future__ import annotations

import pytest

from app.knowledge.guard import (
    GuardState,
    IndexDecision,
    compute_index_input_hash,
    decide,
)
from app.knowledge.ingest import resolve_index_version
from app.models.index_guard import LIFECYCLE_ACTIVE, LIFECYCLE_DELETED, LIFECYCLE_QUARANTINED

TENANT = 1
HASH_A = "a" * 64
HASH_B = "b" * 64


def _state(
    *,
    tenant_id: int = TENANT,
    lifecycle: str = LIFECYCLE_ACTIVE,
    current: int | None = None,
    digest: str | None = None,
) -> GuardState:
    return GuardState(
        document_id=1,
        tenant_id=tenant_id,
        lifecycle_status=lifecycle,
        current_index_version=current,
        index_input_hash=digest,
    )


# --------------------------------------------------------------------------- #
# decide：版本判定矩阵
# --------------------------------------------------------------------------- #
def test_decide_apply_when_no_guard_row() -> None:
    assert decide(None, tenant_id=TENANT, index_version=1, input_hash=HASH_A) is IndexDecision.APPLY


def test_decide_apply_when_current_is_null() -> None:
    """guard 存在但版本未知（存量回填 / 首次 ingest）→ APPLY。"""
    assert (
        decide(
            _state(current=None), tenant_id=TENANT, index_version=1, input_hash=HASH_A
        )
        is IndexDecision.APPLY
    )


def test_decide_idempotent_same_version_same_hash() -> None:
    assert (
        decide(
            _state(current=3, digest=HASH_A),
            tenant_id=TENANT,
            index_version=3,
            input_hash=HASH_A,
        )
        is IndexDecision.IDEMPOTENT
    )


def test_decide_reject_version_mismatch_same_version_other_hash() -> None:
    assert (
        decide(
            _state(current=3, digest=HASH_A),
            tenant_id=TENANT,
            index_version=3,
            input_hash=HASH_B,
        )
        is IndexDecision.REJECT_VERSION_MISMATCH
    )


def test_decide_reject_stale() -> None:
    assert (
        decide(
            _state(current=5, digest=HASH_A),
            tenant_id=TENANT,
            index_version=4,
            input_hash=HASH_A,
        )
        is IndexDecision.REJECT_STALE
    )


def test_decide_apply_higher_version() -> None:
    assert (
        decide(
            _state(current=5, digest=HASH_A),
            tenant_id=TENANT,
            index_version=6,
            input_hash=HASH_B,
        )
        is IndexDecision.APPLY
    )


def test_decide_reject_deleted_even_with_higher_version() -> None:
    """删除是终态：版本更大也一律拒绝，否则晚到任务会复活索引。"""
    assert (
        decide(
            _state(lifecycle=LIFECYCLE_DELETED, current=1, digest=HASH_A),
            tenant_id=TENANT,
            index_version=99,
            input_hash=HASH_B,
        )
        is IndexDecision.REJECT_DELETED
    )


def test_decide_reject_quarantined() -> None:
    assert (
        decide(
            _state(lifecycle=LIFECYCLE_QUARANTINED, current=1),
            tenant_id=TENANT,
            index_version=2,
            input_hash=HASH_A,
        )
        is IndexDecision.REJECT_QUARANTINED
    )


def test_decide_reject_tenant_mismatch() -> None:
    assert (
        decide(
            _state(tenant_id=TENANT + 1, current=1),
            tenant_id=TENANT,
            index_version=2,
            input_hash=HASH_A,
        )
        is IndexDecision.REJECT_TENANT_MISMATCH
    )


def test_decide_ignore_input_hash_makes_same_version_idempotent() -> None:
    """补向量路径（embedding task）不比输入摘要：同版本即幂等。"""
    assert (
        decide(
            _state(current=2, digest=HASH_A),
            tenant_id=TENANT,
            index_version=2,
            input_hash=HASH_B,
            ignore_input_hash=True,
        )
        is IndexDecision.IDEMPOTENT
    )


def test_decide_deleted_beats_ignore_input_hash() -> None:
    """忽略输入摘要不能放宽生命周期。"""
    assert (
        decide(
            _state(lifecycle=LIFECYCLE_DELETED, current=2, digest=HASH_A),
            tenant_id=TENANT,
            index_version=2,
            input_hash=HASH_A,
            ignore_input_hash=True,
        )
        is IndexDecision.REJECT_DELETED
    )


@pytest.mark.parametrize(
    "decision",
    [
        IndexDecision.REJECT_DELETED,
        IndexDecision.REJECT_QUARANTINED,
        IndexDecision.REJECT_STALE,
        IndexDecision.REJECT_VERSION_MISMATCH,
        IndexDecision.REJECT_TENANT_MISMATCH,
    ],
)
def test_rejected_property(decision: IndexDecision) -> None:
    assert decision.rejected is True


@pytest.mark.parametrize("decision", [IndexDecision.APPLY, IndexDecision.IDEMPOTENT])
def test_not_rejected_property(decision: IndexDecision) -> None:
    assert decision.rejected is False


# --------------------------------------------------------------------------- #
# compute_index_input_hash
# --------------------------------------------------------------------------- #
def _hash(**overrides) -> str:
    base = dict(
        file_hash="f1",
        document_type="historical_bid",
        knowledge_base_id=10,
        parser="docx",
        chunk_max_chars=1200,
        chunk_overlap=150,
    )
    base.update(overrides)
    return compute_index_input_hash(**base)  # type: ignore[arg-type]


def test_input_hash_is_deterministic() -> None:
    assert _hash() == _hash()


def test_input_hash_is_64_hex() -> None:
    digest = _hash()
    assert len(digest) == 64
    int(digest, 16)  # 可解析为十六进制


@pytest.mark.parametrize(
    "overrides",
    [
        {"file_hash": "f2"},
        {"document_type": "tender"},
        {"knowledge_base_id": 11},
        {"parser": "pdf_text"},
        {"chunk_max_chars": 800},
        {"chunk_overlap": 0},
    ],
)
def test_input_hash_changes_with_index_affecting_inputs(overrides: dict) -> None:
    assert _hash(**overrides) != _hash()


def test_input_hash_extra_keys_are_order_insensitive() -> None:
    a = compute_index_input_hash(
        file_hash="f", document_type="d", knowledge_base_id=1, parser="p", extra={"x": 1, "y": 2}
    )
    b = compute_index_input_hash(
        file_hash="f", document_type="d", knowledge_base_id=1, parser="p", extra={"y": 2, "x": 1}
    )
    assert a == b


# --------------------------------------------------------------------------- #
# resolve_index_version
# --------------------------------------------------------------------------- #
def test_resolve_index_version_prefers_parse_log_id() -> None:
    assert resolve_index_version(index_version=None, parse_log_id=7) == 7
    assert resolve_index_version(index_version=7, parse_log_id=7) == 7


def test_resolve_index_version_rejects_mismatch() -> None:
    from app.core.tenant import TenantScopeError

    with pytest.raises(TenantScopeError):
        resolve_index_version(index_version=3, parse_log_id=5)


def test_resolve_index_version_defaults_to_one_without_parse_log_id() -> None:
    assert resolve_index_version(index_version=None, parse_log_id=None) == 1


@pytest.mark.parametrize("bad", [0, -1])
def test_resolve_index_version_rejects_non_positive(bad: int) -> None:
    from app.core.tenant import TenantScopeError

    with pytest.raises(TenantScopeError):
        resolve_index_version(index_version=bad, parse_log_id=None)


# --------------------------------------------------------------------------- #
# 请求契约：删除索引接口
# --------------------------------------------------------------------------- #
def test_delete_index_request_requires_tenant_and_document() -> None:
    from pydantic import ValidationError

    from app.api.schemas import DeleteIndexRequest

    with pytest.raises(ValidationError):
        DeleteIndexRequest.model_validate({"documentId": 1})
    with pytest.raises(ValidationError):
        DeleteIndexRequest.model_validate({"tenantId": 1})
    assert DeleteIndexRequest.model_validate({"tenantId": 1, "documentId": 2}).document_id == 2


def test_delete_index_request_accepts_snake_case_tenant() -> None:
    from app.api.schemas import DeleteIndexRequest

    assert DeleteIndexRequest.model_validate({"tenant_id": 9, "documentId": 2}).tenant_id == 9


def test_delete_index_request_rejects_conflicting_tenant() -> None:
    from pydantic import ValidationError

    from app.api.schemas import DeleteIndexRequest

    with pytest.raises(ValidationError):
        DeleteIndexRequest.model_validate({"tenantId": 1, "tenant_id": 2, "documentId": 3})


def test_parse_request_rejects_index_version_parse_log_mismatch() -> None:
    from pydantic import ValidationError

    from app.api.schemas import ParseRequest

    with pytest.raises(ValidationError):
        ParseRequest.model_validate(
            {"documentId": 1, "persist": True, "tenantId": 1, "parseLogId": 3, "indexVersion": 4}
        )
    # 一致时通过
    req = ParseRequest.model_validate(
        {"documentId": 1, "persist": True, "tenantId": 1, "parseLogId": 3, "indexVersion": 3}
    )
    assert req.index_version == 3 and req.parse_log_id == 3


@pytest.fixture(scope="module")
def http_client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_http_delete_endpoint_rejects_missing_tenant(http_client) -> None:
    assert (
        http_client.post("/api/bidox/document/delete", json={"documentId": 1}).status_code == 422
    )


def test_http_delete_endpoint_rejects_conflicting_tenant(http_client) -> None:
    response = http_client.post(
        "/api/bidox/document/delete",
        json={"documentId": 1, "tenantId": 1, "tenant_id": 2},
    )
    assert response.status_code == 422
