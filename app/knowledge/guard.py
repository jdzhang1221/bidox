"""索引生命周期守卫协议（批次 A / §4.3）。

状态机
------
`lifecycle_status`: ACTIVE → DELETED（**终态**，恢复必须新建 document_id）；
ACTIVE → QUARANTINED（人工隔离，同样拒绝普通 ingest）。

版本判定（`current_index_version` 是**已提交**版本）
----------------------------------------------------
| 情形 | 判定 |
| --- | --- |
| lifecycle = DELETED | `REJECT_DELETED`（即便版本更大，普通 ingest 永远拒绝） |
| lifecycle = QUARANTINED | `REJECT_QUARANTINED` |
| current 为空 | `APPLY`（首次） |
| version <  current | `REJECT_STALE`（旧任务晚到） |
| version == current 且 hash 相同 | `IDEMPOTENT`（幂等重试，**不重建** section/chunk） |
| version == current 且 hash 不同 | `REJECT_VERSION_MISMATCH` |
| version >  current | `APPLY`（允许替换） |

锁协议
------
`ingest` / `delete` 都先 `INSERT ... ON CONFLICT DO NOTHING` 建立 guard，再
`SELECT ... FOR UPDATE`。**不得**用伪造的 document 字段去建 tombstone —— guard 行本身
就能在 document 行不存在（首次 ingest）或已被清掉（删除后）时承担版本与生命周期的权威。

写事务纪律
----------
embedding / reranker / pattern 抽取都在**行锁外**完成；持锁的短事务里只做
「重新校验版本与 lifecycle → 原子替换 → 提交版本」。这样慢操作不会长时间占锁，
而并发删除/更高版本仍能在提交点被拦下。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.tenant import TenantScopeError, require_tenant_id
from app.models.index_guard import (
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_OBSOLETE,
    JOB_PENDING,
    JOB_RUNNING,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DELETED,
    DocumentIndexGuard,
    DocumentPatternJob,
    active_document_clause,
)

# 从 models 层重导出：调用方（含检索层）统一从这里取，避免两处 import 路径
__all__ = [
    "DEFAULT_JOB_LEASE_SECONDS",
    "GuardState",
    "IndexDecision",
    "IndexRejected",
    "active_document_clause",
    "begin_index",
    "claim_pattern_job",
    "commit_index",
    "complete_pattern_job",
    "compute_index_input_hash",
    "count_guards_by_lifecycle",
    "decide",
    "ensure_guard",
    "fail_pattern_job",
    "lock_guard",
    "obsolete_all_live_jobs",
    "obsolete_jobs_below",
    "pattern_job_is_current",
    "purge_document_index",
    "read_guard",
    "tombstone",
]

logger = get_logger(__name__)

# pattern 抽取任务默认租约（秒）：超时后允许其他持有者接管，避免崩溃后永久卡死
DEFAULT_JOB_LEASE_SECONDS = 900


class IndexDecision(str, Enum):
    """`begin_index` 的判定结果。"""

    APPLY = "APPLY"  # 允许写入（首次 / 更高版本）
    IDEMPOTENT = "IDEMPOTENT"  # 同版本同 hash 且已提交：跳过重建
    REJECT_DELETED = "REJECT_DELETED"  # 生命周期为 DELETED
    REJECT_QUARANTINED = "REJECT_QUARANTINED"  # 生命周期为 QUARANTINED
    REJECT_STALE = "REJECT_STALE"  # index_version 落后于已提交版本
    REJECT_VERSION_MISMATCH = "REJECT_VERSION_MISMATCH"  # 同版本但输入不同
    REJECT_TENANT_MISMATCH = "REJECT_TENANT_MISMATCH"  # guard 归属别的租户

    @property
    def rejected(self) -> bool:
        return self in {
            IndexDecision.REJECT_DELETED,
            IndexDecision.REJECT_QUARANTINED,
            IndexDecision.REJECT_STALE,
            IndexDecision.REJECT_VERSION_MISMATCH,
            IndexDecision.REJECT_TENANT_MISMATCH,
        }


class IndexRejected(Exception):
    """索引写入被守卫拒绝。携带判定结果，便于调用方映射成 4xx / 跳过。"""

    def __init__(self, decision: IndexDecision, message: str) -> None:
        super().__init__(message)
        self.decision = decision


@dataclass(frozen=True)
class GuardState:
    """guard 行快照。"""

    document_id: int
    tenant_id: int
    lifecycle_status: str
    current_index_version: int | None
    index_input_hash: str | None

    @property
    def committed(self) -> bool:
        """是否已有已提交版本。"""
        return self.current_index_version is not None


# --------------------------------------------------------------------------- #
# 输入摘要
# --------------------------------------------------------------------------- #
def compute_index_input_hash(
    *,
    file_hash: str | None,
    document_type: str | None,
    knowledge_base_id: int | None,
    parser: str | None,
    chunk_max_chars: int | None = None,
    chunk_overlap: int | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    """计算「影响索引结果」的输入摘要（SHA-256）。

    只包含**稳定**输入：原文件内容哈希、文档类型、知识库、解析器与切分配置。
    **绝不**包含临时 URL / storageKey —— 同一个文件换一次下载链接就变 hash，
    会把本该幂等的重试误判成「同版本不同输入」而拒绝。
    """
    parts = [
        f"file_hash={file_hash or ''}",
        f"document_type={document_type or ''}",
        f"knowledge_base_id={knowledge_base_id if knowledge_base_id is not None else ''}",
        f"parser={parser or ''}",
        f"chunk_max_chars={chunk_max_chars if chunk_max_chars is not None else ''}",
        f"chunk_overlap={chunk_overlap if chunk_overlap is not None else ''}",
    ]
    for key in sorted((extra or {}).keys()):
        parts.append(f"{key}={extra[key]}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# guard 基础操作
# --------------------------------------------------------------------------- #
def ensure_guard(
    session: Session,
    *,
    document_id: int,
    tenant_id: int,
    lifecycle_status: str = LIFECYCLE_ACTIVE,
) -> None:
    """幂等建立 guard 行（`INSERT ... ON CONFLICT DO NOTHING`）。

    **不修改**已存在的行：已 DELETED 的文档不会因为一次 ingest 又变回 ACTIVE。
    """
    tenant_id = require_tenant_id(tenant_id, where="ensure_guard")
    stmt = (
        pg_insert(DocumentIndexGuard)
        .values(
            document_id=document_id,
            tenant_id=tenant_id,
            lifecycle_status=lifecycle_status,
            current_index_version=None,
            index_input_hash=None,
        )
        .on_conflict_do_nothing(index_elements=[DocumentIndexGuard.document_id])
    )
    session.execute(stmt)


def lock_guard(session: Session, *, document_id: int) -> GuardState | None:
    """`SELECT ... FOR UPDATE` 取 guard 行（持锁到事务结束）。不存在返回 None。"""
    row = session.execute(
        select(DocumentIndexGuard)
        .where(DocumentIndexGuard.document_id == document_id)
        .with_for_update()
    ).scalar_one_or_none()
    return _to_state(row) if row is not None else None


def read_guard(session: Session, *, document_id: int) -> GuardState | None:
    """只读取 guard 行（不加锁），用于展示 / 校验。"""
    row = session.get(DocumentIndexGuard, document_id)
    return _to_state(row) if row is not None else None


def _to_state(row: DocumentIndexGuard) -> GuardState:
    return GuardState(
        document_id=row.document_id,
        tenant_id=row.tenant_id,
        lifecycle_status=row.lifecycle_status,
        current_index_version=row.current_index_version,
        index_input_hash=row.index_input_hash,
    )


def decide(
    state: GuardState | None,
    *,
    tenant_id: int,
    index_version: int,
    input_hash: str,
    ignore_input_hash: bool = False,
) -> IndexDecision:
    """纯函数版版本判定（便于单测）。

    ignore_input_hash=True 时，`version == current` 一律判 IDEMPOTENT，不做输入比对。
    用于「只补向量、不改结构」的路径（如 `handle_embedding_task`）：同版本重跑本就是空操作，
    再拿输入摘要去比只会因为摘要口径不同而误拒。
    """
    if state is None:
        return IndexDecision.APPLY
    if state.tenant_id != tenant_id:
        return IndexDecision.REJECT_TENANT_MISMATCH
    if state.lifecycle_status == LIFECYCLE_DELETED:
        return IndexDecision.REJECT_DELETED
    if state.lifecycle_status != LIFECYCLE_ACTIVE:
        return IndexDecision.REJECT_QUARANTINED
    current = state.current_index_version
    if current is None:
        return IndexDecision.APPLY
    if index_version < current:
        return IndexDecision.REJECT_STALE
    if index_version == current:
        if ignore_input_hash:
            return IndexDecision.IDEMPOTENT
        return (
            IndexDecision.IDEMPOTENT
            if state.index_input_hash == input_hash
            else IndexDecision.REJECT_VERSION_MISMATCH
        )
    return IndexDecision.APPLY


def begin_index(
    session: Session,
    *,
    tenant_id: int,
    document_id: int,
    index_version: int,
    input_hash: str,
    require_existing: bool = False,
    ignore_input_hash: bool = False,
) -> tuple[GuardState | None, IndexDecision]:
    """建立 guard + 行锁 + 版本判定。**必须在写事务内调用**。

    require_existing=True 时，若 guard 行不存在则直接判定 REJECT_TENANT_MISMATCH —— 用于
    「写事务内重新校验」的场景：此时 guard 一定已由第一次 begin_index 建好，
    若反而不见了，说明期间发生了清理，必须拒绝而不是重建。
    """
    tenant_id = require_tenant_id(tenant_id, where="begin_index")
    if index_version is None or index_version <= 0:
        raise TenantScopeError(f"begin_index: index_version 非法({index_version!r})")

    if not require_existing:
        ensure_guard(session, document_id=document_id, tenant_id=tenant_id)
    state = lock_guard(session, document_id=document_id)
    if state is None:
        return None, IndexDecision.REJECT_TENANT_MISMATCH
    return state, decide(
        state,
        tenant_id=tenant_id,
        index_version=index_version,
        input_hash=input_hash,
        ignore_input_hash=ignore_input_hash,
    )


def commit_index(
    session: Session,
    *,
    document_id: int,
    index_version: int,
    input_hash: str,
) -> None:
    """提交版本：把 guard 的 current_index_version / index_input_hash 推到本次版本。

    必须与「写入 section/chunk」**同一个事务**，否则会出现「数据已换、版本没动」
    导致后续同版本重试被误判为版本冲突。
    """
    result = session.execute(
        update(DocumentIndexGuard)
        .where(DocumentIndexGuard.document_id == document_id)
        .values(current_index_version=index_version, index_input_hash=input_hash)
    )
    if result.rowcount != 1:
        raise IndexRejected(
            IndexDecision.REJECT_TENANT_MISMATCH,
            f"commit_index: guard 行不存在（document_id={document_id}）",
        )


def tombstone(session: Session, *, tenant_id: int, document_id: int) -> bool:
    """幂等写删除墓碑：guard.lifecycle_status → DELETED。

    返回 True 表示本次调用完成了状态跃迁（首次删除），False 表示本来就是 DELETED（重复调用）。
    **不**清空 current_index_version —— 保留用于审计，且 DELETED 本身已能拒绝一切 ingest。
    """
    tenant_id = require_tenant_id(tenant_id, where="tombstone")
    ensure_guard(session, document_id=document_id, tenant_id=tenant_id)
    state = lock_guard(session, document_id=document_id)
    if state is None:  # pragma: no cover - ensure_guard 刚建过，防御性
        raise TenantScopeError(f"tombstone: guard 行建立失败（document_id={document_id}）")
    if state.tenant_id != tenant_id:
        raise TenantScopeError(
            f"tombstone: document {document_id} 属于租户 {state.tenant_id}，"
            f"拒绝被租户 {tenant_id} 删除"
        )
    if state.lifecycle_status == LIFECYCLE_DELETED:
        return False
    session.execute(
        update(DocumentIndexGuard)
        .where(DocumentIndexGuard.document_id == document_id)
        .values(lifecycle_status=LIFECYCLE_DELETED)
    )
    return True


# --------------------------------------------------------------------------- #
# pattern 抽取任务
# --------------------------------------------------------------------------- #
def claim_pattern_job(
    session: Session,
    *,
    tenant_id: int,
    document_id: int,
    index_version: int,
    input_hash: str,
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
    owner_id: UUID | None = None,
) -> UUID | None:
    """抢占 pattern 抽取任务。返回 owner_id 表示抢到；None 表示已被别人持有。

    已完成的任务不重跑；租约过期的 RUNNING 允许被接管（崩溃恢复）。
    """
    tenant_id = require_tenant_id(tenant_id, where="claim_pattern_job")
    now = datetime.now(timezone.utc)
    owner = owner_id or uuid4()

    session.execute(
        pg_insert(DocumentPatternJob)
        .values(
            tenant_id=tenant_id,
            document_id=document_id,
            index_version=index_version,
            input_hash=input_hash,
            state=JOB_RUNNING,
            owner_id=owner,
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        .on_conflict_do_nothing(
            index_elements=[
                DocumentPatternJob.tenant_id,
                DocumentPatternJob.document_id,
                DocumentPatternJob.index_version,
            ]
        )
    )
    row = session.execute(
        select(DocumentPatternJob)
        .where(
            DocumentPatternJob.tenant_id == tenant_id,
            DocumentPatternJob.document_id == document_id,
            DocumentPatternJob.index_version == index_version,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:  # pragma: no cover - 防御性
        return None

    if row.state == JOB_COMPLETED:
        return None
    if row.state == JOB_RUNNING and row.owner_id != owner:
        expires = row.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is not None and expires > now:
            return None  # 别人还活着

    session.execute(
        update(DocumentPatternJob)
        .where(
            DocumentPatternJob.tenant_id == tenant_id,
            DocumentPatternJob.document_id == document_id,
            DocumentPatternJob.index_version == index_version,
        )
        .values(
            state=JOB_RUNNING,
            owner_id=owner,
            input_hash=input_hash,
            expires_at=now + timedelta(seconds=lease_seconds),
            error_message=None,
        )
    )
    return owner


def pattern_job_is_current(
    session: Session,
    *,
    tenant_id: int,
    document_id: int,
    index_version: int,
    owner_id: UUID,
) -> bool:
    """保存 pattern 前的最终校验：guard 与 job 都必须指向本次 (version, owner)。

    这是「V1 运行中提交了 V2」的唯一正确判定点 —— V1 跑完时 guard.current 已是 2，
    于是 V1 的 pattern 被丢弃，不会污染 V2 的索引。
    """
    state = read_guard(session, document_id=document_id)
    if state is None:
        return False
    if state.tenant_id != tenant_id or state.lifecycle_status != LIFECYCLE_ACTIVE:
        return False
    if state.current_index_version != index_version:
        return False
    row = session.execute(
        select(DocumentPatternJob).where(
            DocumentPatternJob.tenant_id == tenant_id,
            DocumentPatternJob.document_id == document_id,
            DocumentPatternJob.index_version == index_version,
        )
    ).scalar_one_or_none()
    return row is not None and row.state == JOB_RUNNING and row.owner_id == owner_id


def complete_pattern_job(
    session: Session,
    *,
    tenant_id: int,
    document_id: int,
    index_version: int,
    owner_id: UUID | None = None,
) -> bool:
    """标记任务完成。**必须与 pattern 写入同一事务**，否则会出现
    「pattern 已写但 job 还是 RUNNING」→ 重试重复写入。"""
    conditions = [
        DocumentPatternJob.tenant_id == tenant_id,
        DocumentPatternJob.document_id == document_id,
        DocumentPatternJob.index_version == index_version,
    ]
    if owner_id is not None:
        conditions.append(DocumentPatternJob.owner_id == owner_id)
    result = session.execute(
        update(DocumentPatternJob)
        .where(*conditions)
        .values(state=JOB_COMPLETED, expires_at=None)
    )
    return result.rowcount == 1


def fail_pattern_job(
    session: Session,
    *,
    tenant_id: int,
    document_id: int,
    index_version: int,
    error_message: str | None = None,
) -> None:
    """标记任务失败（可重试：下次 claim 会重新拉起）。"""
    session.execute(
        update(DocumentPatternJob)
        .where(
            DocumentPatternJob.tenant_id == tenant_id,
            DocumentPatternJob.document_id == document_id,
            DocumentPatternJob.index_version == index_version,
        )
        .values(state=JOB_FAILED, error_message=(error_message or "")[:1024], expires_at=None)
    )


def obsolete_jobs_below(
    session: Session, *, tenant_id: int, document_id: int, current_index_version: int
) -> int:
    """把低于当前版本的未完成任务标为 OBSOLETE（清理用，不影响正确性）。"""
    result = session.execute(
        update(DocumentPatternJob)
        .where(
            DocumentPatternJob.tenant_id == tenant_id,
            DocumentPatternJob.document_id == document_id,
            DocumentPatternJob.index_version < current_index_version,
            DocumentPatternJob.state.not_in([JOB_COMPLETED, JOB_OBSOLETE]),
        )
        .values(state=JOB_OBSOLETE)
    )
    return result.rowcount


def obsolete_all_live_jobs(session: Session, *, tenant_id: int, document_id: int) -> int:
    """把某文档所有未完成任务标为 OBSOLETE（删除文档时调用）。"""
    result = session.execute(
        update(DocumentPatternJob)
        .where(
            DocumentPatternJob.tenant_id == tenant_id,
            DocumentPatternJob.document_id == document_id,
            DocumentPatternJob.state.in_([JOB_PENDING, JOB_RUNNING]),
        )
        .values(state=JOB_OBSOLETE, expires_at=None)
    )
    return result.rowcount


def purge_document_index(session: Session, *, document_id: int) -> dict[str, int]:
    """清理某文档的全部索引数据（section / chunk / pattern_source）。

    pattern 本体只在「没有其他有效来源」时才删除，避免误删跨文档共享的方案组件。
    **不含 guard 行** —— guard 由 tombstone 单独处理，必须留存。
    """
    from app.models.chunk import DocumentChunk
    from app.models.pattern import PatternSource, SolutionPattern
    from app.models.section import DocumentSection

    pattern_ids = list(
        session.execute(
            select(PatternSource.pattern_id)
            .where(PatternSource.document_id == document_id)
            .distinct()
        ).scalars()
    )
    src_deleted = session.execute(
        delete(PatternSource).where(PatternSource.document_id == document_id)
    ).rowcount
    chunk_deleted = session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    ).rowcount
    section_deleted = session.execute(
        delete(DocumentSection).where(DocumentSection.document_id == document_id)
    ).rowcount

    orphan_patterns = 0
    if pattern_ids:
        # 仍有其他来源的 pattern 保留；只有彻底无来源的才删
        remaining = set(
            session.execute(
                select(PatternSource.pattern_id)
                .where(PatternSource.pattern_id.in_(pattern_ids))
                .distinct()
            ).scalars()
        )
        orphan = [pid for pid in pattern_ids if pid not in remaining]
        if orphan:
            orphan_patterns = session.execute(
                delete(SolutionPattern).where(SolutionPattern.id.in_(orphan))
            ).rowcount

    return {
        "pattern_source": int(src_deleted or 0),
        "document_chunk": int(chunk_deleted or 0),
        "document_section": int(section_deleted or 0),
        "solution_pattern": int(orphan_patterns or 0),
    }


def count_guards_by_lifecycle(session: Session, *, tenant_id: int) -> dict[str, int]:
    """按生命周期统计 guard 数量（验收/运维用）。"""
    rows = session.execute(
        select(DocumentIndexGuard.lifecycle_status, func.count())
        .where(DocumentIndexGuard.tenant_id == tenant_id)
        .group_by(DocumentIndexGuard.lifecycle_status)
    ).all()
    return {status: count for status, count in rows}

