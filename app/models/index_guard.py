"""索引生命周期守卫表（批次 A / §4.3）。

为什么不能复用 `document.status`
------------------------------
`document.status`（pending/parsing/parsed/embedded/failed）描述的是**解析进度**，无法承担
「该文档当前允许哪个 `index_version` 落库」的职责：

- 首次 ingest 时 `document` 行可能还不存在，**没有行就无从加锁**；
- 删除意图必须在 `document` 行被清掉之后依然可查，否则晚到任务会复活索引；
- 解析状态每次尝试都变，而版本权威必须单调且可原子比较。

因此引入 `document_index_guard`：一行一文档，`SELECT ... FOR UPDATE` 行锁，
是「版本 + 生命周期」的唯一权威。`document_pattern_job` 则把 pattern 抽取任务
按 `(tenant, document, indexVersion)` 持久化，使「V1 运行时提交 V2」不再靠内存标记判正确性。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    String,
    Uuid,
    exists,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 生命周期：ACTIVE → DELETED（终态，恢复必须新建 document_id）；QUARANTINED 为人工隔离。
LIFECYCLE_ACTIVE = "ACTIVE"
LIFECYCLE_DELETED = "DELETED"
LIFECYCLE_QUARANTINED = "QUARANTINED"
LIFECYCLE_STATUSES = (LIFECYCLE_ACTIVE, LIFECYCLE_DELETED, LIFECYCLE_QUARANTINED)

# pattern 抽取任务状态
JOB_PENDING = "PENDING"
JOB_RUNNING = "RUNNING"
JOB_COMPLETED = "COMPLETED"
JOB_FAILED = "FAILED"
JOB_OBSOLETE = "OBSOLETE"
JOB_STATES = (JOB_PENDING, JOB_RUNNING, JOB_COMPLETED, JOB_FAILED, JOB_OBSOLETE)


class DocumentIndexGuard(Base):
    """文档索引守卫（一行一文档）。

    `document_id` 为 PK 且**不自增**：由调用方（Java 文档编号）传入。
    """

    __tablename__ = "document_index_guard"

    document_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    lifecycle_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LIFECYCLE_ACTIVE
    )
    # 已提交（committed）的索引版本；NULL 表示「尚无已知版本」（首次 ingest 或历史数据）
    current_index_version: Mapped[int | None] = mapped_column(BigInteger)
    # 影响索引的输入摘要（原文件内容 + 解析配置 + 文档类型 + 知识库等），SHA-256
    index_input_hash: Mapped[str | None] = mapped_column(String(64))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('ACTIVE', 'DELETED', 'QUARANTINED')",
            name="ck_document_index_guard_lifecycle",
        ),
    )


class DocumentPatternJob(Base):
    """pattern 抽取任务（按租户 + 文档 + 索引版本唯一）。

    持久化的目的是让「V1 任务在 V2 已提交后才跑完」这件事**可判定**：
    保存 pattern 前查一次 job + guard，两者都指向自己这次 `index_version` 才允许写入。
    内存里的 `_extracting_docs` 只作并发优化，不作为正确性依据。
    """

    __tablename__ = "document_pattern_job"

    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    document_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    index_version: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=JOB_PENDING)
    # 持有者（进程内 UUID）：租约未过期时其他持有者不得抢占
    owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(String(1024))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'OBSOLETE')",
            name="ck_document_pattern_job_state",
        ),
    )


def active_document_clause(document_id_column: Any):
    """返回「该 document 的 guard 生命周期为 ACTIVE」的 EXISTS 条件，供检索层使用。

    放在 models 层而不是 `app/knowledge/guard.py`，是为了避开
    `retrieval → knowledge.__init__ → knowledge.service → retrieval` 的循环导入。

    用于排除 `DELETED` / `QUARANTINED` 文档，是 §4.1「tenant、生命周期、版本、类型过滤仍必须存在」
    里「生命周期」这一项的落地点：

    - `DELETED`：墓碑写入后 section/chunk 已被清空，本来也检不到，这里是**双保险**
      （防止清理部分失败时残留数据被召回）；
    - `QUARANTINED`：人工隔离的文档必须立刻从检索结果消失。

    **没有 guard 行的文档不会命中** —— 未纳入生命周期管理就不该被检索到。
    生产数据在 §4.3 迁移中已为全部存量 document 回填 guard 行。
    """
    return exists(
        select(1)
        .select_from(DocumentIndexGuard)
        .where(
            DocumentIndexGuard.document_id == document_id_column,
            DocumentIndexGuard.lifecycle_status == LIFECYCLE_ACTIVE,
        )
    )
