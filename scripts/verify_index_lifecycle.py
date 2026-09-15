"""批次 A §4.6 验收：索引生命周期（真实 PostgreSQL）。

验收原文（开发计划 §4.6）::

    首次 ingest 与 delete 并发，删除后任何晚到任务均不能复活索引。
    同版本重试不重建 section；V1 pattern 运行时提交 V2，V2 pattern 最终存在且不重复。
    真实 PostgreSQL 验证版本锁、partial index、UUID、事务回滚。

为什么必须打真库
----------------
守卫协议的正确性建立在三件**只有真实 PostgreSQL 才有**的东西上：

- `INSERT ... ON CONFLICT DO NOTHING` 与 `SELECT ... FOR UPDATE` 的**行锁语义**；
- `document_pattern_job` 的**复合主键**与 `owner_id uuid`；
- 事务回滚后「数据与版本一起回退」的原子性。

H2 / SQLite / 假 session 都验不了这些，所以本脚本直连 `bidox_ai`。

隔离策略
--------
用专用测试租户 `TENANT=7` 与哨兵文档号段 `9100001+`，跑前跑后都清理，
不影响真实数据（租户 1）。所有断言都带 tenant 边界，与生产代码同路径。

用法::

    uv run python scripts/verify_index_lifecycle.py
    uv run python scripts/verify_index_lifecycle.py --keep-data   # 排查用，保留测试数据

退出码：0 = 全部通过；1 = 存在失败项。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text

from app.core.database import get_session, session_scope
from app.core.logging import get_logger, setup_logging
from app.document.ast.models import Document
from app.document.chunk.models import Chunk
from app.document.pipeline import ParseResult
from app.document.section.models import Section
from app.knowledge.guard import (
    GuardState,
    IndexDecision,
    IndexRejected,
    begin_index,
    claim_pattern_job,
    commit_index,
    compute_index_input_hash,
    ensure_guard,
    lock_guard,
    pattern_job_is_current,
    read_guard,
    tombstone,
)
from app.knowledge.indexer import KnowledgeIndexer
from app.knowledge.ingest import delete_document_index, ingest_result
from app.models.chunk import DocumentChunk
from app.models.index_guard import (
    JOB_COMPLETED,
    JOB_OBSOLETE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DELETED,
    DocumentIndexGuard,
    DocumentPatternJob,
)
from app.models.pattern import PatternSource, SolutionPattern
from app.models.section import DocumentSection
from app.pattern.indexer import SolutionPatternIndexer
from app.pattern.models import SolutionPattern as SolutionPatternPayload

logger = get_logger(__name__)

TENANT = 7  # 专用测试租户，绝不与真实数据（租户 1）混
DOC_SEQ_START = 9_100_001


# --------------------------------------------------------------------------- #
# 断言收集
# --------------------------------------------------------------------------- #
@dataclass
class Results:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if detail:
            print(f"         {detail}")
        return bool(ok)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]


# --------------------------------------------------------------------------- #
# 测试数据构造
# --------------------------------------------------------------------------- #
def _make_result(doc_id: int, *, sections: int = 3, chunks_per_section: int = 2) -> ParseResult:
    """构造一份确定性的最小 ParseResult（含父子章节，便于验 section 替换）。"""
    root = Section(title="第一章 总体方案", level=1, section_no="1", content="总体方案正文。")
    child_a = Section(title="1.1 技术路线", level=2, section_no="1.1", content="技术路线正文。")
    child_b = Section(title="1.2 实施计划", level=2, section_no="1.2", content="实施计划正文。")
    root.children = [child_a, child_b][: max(1, sections - 1)]

    flat: list[Section] = []
    for sec in [root] + root.children:
        flat.append(sec)

    chunks: list[Chunk] = []
    idx = 0
    for sec in flat:
        for _ in range(chunks_per_section):
            chunks.append(
                Chunk(
                    document_id=doc_id,
                    section_id=sec.id,
                    chunk_index=idx,
                    chunk_type="paragraph",
                    title=sec.title,
                    content=f"{sec.title} 的第 {idx} 段内容（生命周期验收用）。",
                    document_type="historical_bid",
                    section_type="technical_solution",
                    page_start=1,
                    page_end=1,
                )
            )
            idx += 1
    # flatten 会重写 sec.id / parent_id，必须在构造 chunk 之后调用并回填
    from app.document.section.models import flatten_sections_with_parent

    ordered = flatten_sections_with_parent([root])
    id_by_obj = {id(s): s.id for s in ordered}
    for c, sec in zip(chunks, [s for s in ordered for _ in range(chunks_per_section)]):
        c.section_id = id_by_obj[id(sec)]

    return ParseResult(
        document=Document(meta={"detected_type": "docx"}),
        sections=[root],
        chunks=chunks,
    )


class _StubEmbedding:
    """固定向量：避免依赖 Ollama，同时保证 pgvector 列有确定值。"""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        # 简单确定性哈希 → 单位向量，让相同文本得到相同向量
        h = abs(hash(text)) % 10_000
        vec = [0.0] * self.dim
        vec[h % self.dim] = 1.0
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def _patch_embedding() -> None:
    """把索引器的 embedding 换成桩，脚本跑得快且不依赖外部服务。"""
    stub = _StubEmbedding()

    def _stub_indexer_init(self) -> None:  # type: ignore[no-untyped-def]
        self._embedding = stub

    def _stub_pattern_init(self, extractor=None) -> None:  # type: ignore[no-untyped-def]
        self._extractor = extractor
        self._embedding = stub

    KnowledgeIndexer.__init__ = _stub_indexer_init  # type: ignore[method-assign]
    SolutionPatternIndexer.__init__ = _stub_pattern_init  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #
def _purge_test_tenant(session) -> None:
    """清掉测试租户的全部痕迹（幂等）。"""
    session.execute(text("delete from pattern_source where document_id >= :d"), {"d": DOC_SEQ_START})
    session.execute(
        text(
            "delete from pattern_source where pattern_id in"
            " (select id from solution_pattern where tenant_id = :t)"
        ),
        {"t": TENANT},
    )
    session.execute(text("delete from solution_pattern where tenant_id = :t"), {"t": TENANT})
    session.execute(text("delete from document_chunk where document_id >= :d"), {"d": DOC_SEQ_START})
    session.execute(
        text("delete from document_section where document_id >= :d"), {"d": DOC_SEQ_START}
    )
    session.execute(
        text("delete from document_pattern_job where tenant_id = :t"), {"t": TENANT}
    )
    session.execute(
        text("delete from document_index_guard where tenant_id = :t"), {"t": TENANT}
    )
    session.execute(text("delete from document where tenant_id = :t"), {"t": TENANT})


def _counts(session, doc_id: int) -> dict[str, int]:
    return {
        "section": session.execute(
            text("select count(*) from document_section where document_id = :d"), {"d": doc_id}
        ).scalar()
        or 0,
        "chunk": session.execute(
            text("select count(*) from document_chunk where document_id = :d"), {"d": doc_id}
        ).scalar()
        or 0,
        "pattern_source": session.execute(
            text("select count(*) from pattern_source where document_id = :d"), {"d": doc_id}
        ).scalar()
        or 0,
    }


def _section_ids(session, doc_id: int) -> list[int]:
    return sorted(
        session.execute(
            text("select id from document_section where document_id = :d"), {"d": doc_id}
        ).scalars()
    )


def _decide(doc_id: int, version: int, digest: str) -> IndexDecision:
    """在独立短事务里跑一次判定（不开写操作）。"""
    with session_scope() as session:
        _, decision = begin_index(
            session,
            tenant_id=TENANT,
            document_id=doc_id,
            index_version=version,
            input_hash=digest,
        )
    return decision


# --------------------------------------------------------------------------- #
# A~E：版本判定矩阵
# --------------------------------------------------------------------------- #
def _run_version_matrix(res: Results) -> int:
    print("\n=== 一、版本判定矩阵（真库 + 行锁）===")
    doc = DOC_SEQ_START
    digest_x = compute_index_input_hash(
        file_hash="aaa", document_type="historical_bid", knowledge_base_id=1, parser="docx"
    )
    digest_y = compute_index_input_hash(
        file_hash="bbb", document_type="historical_bid", knowledge_base_id=1, parser="docx"
    )

    with session_scope() as session:
        _purge_test_tenant(session)

    res.check(
        "首次 ingest 判定为 APPLY",
        _decide(doc, 1, digest_x) is IndexDecision.APPLY,
    )

    with session_scope() as session:
        state = read_guard(session, document_id=doc)
        res.check(
            "ensure_guard 建出 ACTIVE / current=NULL 的 guard 行",
            state is not None
            and state.tenant_id == TENANT
            and state.lifecycle_status == LIFECYCLE_ACTIVE
            and state.current_index_version is None,
            f"state={state}",
        )
        commit_index(session, document_id=doc, index_version=1, input_hash=digest_x)

    with session_scope() as session:
        state = read_guard(session, document_id=doc)
        res.check(
            "commit_index 推进到版本 1",
            state is not None and state.current_index_version == 1,
            f"current={state.current_index_version if state else None}",
        )

    res.check(
        "同版本 + 同输入 → IDEMPOTENT（幂等，不重建）",
        _decide(doc, 1, digest_x) is IndexDecision.IDEMPOTENT,
    )
    res.check(
        "同版本 + 不同输入 → REJECT_VERSION_MISMATCH",
        _decide(doc, 1, digest_y) is IndexDecision.REJECT_VERSION_MISMATCH,
    )

    with session_scope() as session:
        commit_index(session, document_id=doc, index_version=3, input_hash=digest_x)
    res.check(
        "旧版本晚到（v=2 < current=3）→ REJECT_STALE",
        _decide(doc, 2, digest_x) is IndexDecision.REJECT_STALE,
    )
    res.check(
        "更高版本（v=4 > current=3）→ APPLY",
        _decide(doc, 4, digest_x) is IndexDecision.APPLY,
    )
    res.check(
        "别的租户来判定 → REJECT_TENANT_MISMATCH",
        _decide_other_tenant(doc, 4, digest_x) is IndexDecision.REJECT_TENANT_MISMATCH,
    )
    return doc


def _decide_other_tenant(doc_id: int, version: int, digest: str) -> IndexDecision:
    with session_scope() as session:
        _, decision = begin_index(
            session,
            tenant_id=TENANT + 1,  # 故意用别的租户
            document_id=doc_id,
            index_version=version,
            input_hash=digest,
        )
    return decision


# --------------------------------------------------------------------------- #
# B：同版本重试不重建 section（真跑 ingest 两次）
# --------------------------------------------------------------------------- #
def _run_idempotent_ingest(res: Results, doc: int) -> None:
    print("\n=== 二、同版本重试不重建 section（真跑 ingest）===")
    with session_scope() as session:
        _purge_test_tenant(session)

    result = _make_result(doc)
    first = ingest_result(
        result,
        document_id=doc,
        tenant_id=TENANT,
        index_version=1,
        knowledge_base_id=1,
        document_type="historical_bid",
        name="生命周期验收文档",
        file_name="lifecycle.docx",
        file_hash="aaa",
        parser="docx",
        with_patterns=False,
    )
    with session_scope() as session:
        ids_before = _section_ids(session, doc)
        counts_before = _counts(session, doc)

    res.check(
        "首次 ingest 写入 section/chunk 且 applied=True",
        first["applied"] is True
        and counts_before["section"] > 0
        and counts_before["chunk"] > 0,
        f"applied={first['applied']} counts={counts_before}",
    )

    second = ingest_result(
        _make_result(doc),
        document_id=doc,
        tenant_id=TENANT,
        index_version=1,
        knowledge_base_id=1,
        document_type="historical_bid",
        name="生命周期验收文档",
        file_name="lifecycle.docx",
        file_hash="aaa",
        parser="docx",
        with_patterns=False,
    )
    with session_scope() as session:
        ids_after = _section_ids(session, doc)
        counts_after = _counts(session, doc)

    res.check(
        "同版本同输入重试 applied=False（幂等跳过）",
        second["applied"] is False,
        f"applied={second['applied']}",
    )
    res.check(
        "同版本重试 **不重建** section（section id 集合完全不变）",
        ids_before == ids_after,
        f"before={ids_before[:6]}… after={ids_after[:6]}… 数量 {len(ids_before)}→{len(ids_after)}",
    )
    res.check(
        "同版本重试后 section/chunk 数量不变",
        counts_before == counts_after,
        f"before={counts_before} after={counts_after}",
    )


# --------------------------------------------------------------------------- #
# F：删除墓碑 + 晚到任务不复活
# --------------------------------------------------------------------------- #
def _run_delete_tombstone(res: Results, doc: int) -> None:
    print("\n=== 三、删除墓碑 + 晚到任务不复活 ===")
    with session_scope() as session:
        before = _counts(session, doc)

    deleted = delete_document_index(doc, tenant_id=TENANT)
    with session_scope() as session:
        after = _counts(session, doc)
        state = read_guard(session, document_id=doc)

    res.check(
        "删除清理了 section / chunk / pattern_source",
        after["section"] == 0 and after["chunk"] == 0 and after["pattern_source"] == 0,
        f"before={before} after={after} purged={deleted['purged']}",
    )
    res.check(
        "guard 墓碑置为 DELETED（且行仍保留）",
        state is not None and state.lifecycle_status == LIFECYCLE_DELETED,
        f"lifecycle={state.lifecycle_status if state else None}",
    )

    again = delete_document_index(doc, tenant_id=TENANT)
    res.check(
        "重复删除幂等：already_deleted=True",
        again["already_deleted"] is True and again["deleted"] is True,
        f"already_deleted={again['already_deleted']}",
    )

    res.check(
        "删除后更高版本 ingest → REJECT_DELETED（版本更大也不放行）",
        _decide(doc, 99, "whatever") is IndexDecision.REJECT_DELETED,
    )

    # 晚到任务：真的跑一次 ingest，必须被拒且一行都不写
    rejected: IndexDecision | None = None
    try:
        ingest_result(
            _make_result(doc),
            document_id=doc,
            tenant_id=TENANT,
            index_version=99,
            knowledge_base_id=1,
            document_type="historical_bid",
            name="晚到任务",
            file_name="late.docx",
            file_hash="late",
            parser="docx",
            with_patterns=False,
        )
    except IndexRejected as exc:
        rejected = exc.decision
    with session_scope() as session:
        after_late = _counts(session, doc)

    res.check(
        "删除后晚到的 ingest 抛 IndexRejected(REJECT_DELETED)",
        rejected is IndexDecision.REJECT_DELETED,
        f"decision={rejected}",
    )
    res.check(
        "晚到任务未复活索引（各表仍为 0）",
        after_late["section"] == 0 and after_late["chunk"] == 0,
        f"counts={after_late}",
    )

    # 检索侧也必须检不到（生命周期硬边界）
    from app.retrieval.vector import VectorRetriever

    stub = _StubEmbedding()
    hits = VectorRetriever().search(stub.embed_query("x"), tenant_id=TENANT, top_k=50)
    res.check(
        "删除后检索零召回（生命周期硬边界生效）",
        all(h["document_id"] != doc for h in hits),
        f"命中 {len(hits)} 条，含被删文档={any(h['document_id'] == doc for h in hits)}",
    )


# --------------------------------------------------------------------------- #
# G：首次 ingest 与 delete 并发
# --------------------------------------------------------------------------- #
def _run_concurrent_ingest_delete(res: Results, doc: int) -> None:
    print("\n=== 四、首次 ingest 与 delete 并发（删除后不复活）===")
    with session_scope() as session:
        _purge_test_tenant(session)

    embedding_started = threading.Event()
    allow_continue = threading.Event()
    ingest_error: list[BaseException] = []
    ingest_result_data: list[dict[str, Any]] = []

    # 用「卡在阶段 2（向量化）」的方式，把删除精确插进 ingest 的行锁空档
    real_embed = KnowledgeIndexer.embed_chunks

    def _slow_embed(self, chunks):  # type: ignore[no-untyped-def]
        embedding_started.set()
        allow_continue.wait(timeout=30)
        return real_embed(self, chunks)

    KnowledgeIndexer.embed_chunks = _slow_embed  # type: ignore[assignment]

    def _ingest_thread() -> None:
        try:
            ingest_result_data.append(
                ingest_result(
                    _make_result(doc),
                    document_id=doc,
                    tenant_id=TENANT,
                    index_version=1,
                    knowledge_base_id=1,
                    document_type="historical_bid",
                    name="并发文档",
                    file_name="concurrent.docx",
                    file_hash="conc",
                    parser="docx",
                    with_patterns=False,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            ingest_error.append(exc)

    try:
        thread = threading.Thread(target=_ingest_thread, name="ingest-concurrent", daemon=True)
        thread.start()
        if not embedding_started.wait(timeout=30):
            res.check("ingest 已进入向量化阶段（前置条件）", False, "等待超时")
            return

        # 此刻 ingest 已建 guard（ACTIVE）且已释放行锁，正在向量化 → 删除插进来
        delete_document_index(doc, tenant_id=TENANT)
        allow_continue.set()
        thread.join(timeout=60)
    finally:
        KnowledgeIndexer.embed_chunks = real_embed  # type: ignore[assignment]

    with session_scope() as session:
        counts = _counts(session, doc)
        state = read_guard(session, document_id=doc)

    applied = ingest_result_data[0]["applied"] if ingest_result_data else None
    rejected = ingest_error[0].decision if ingest_error and isinstance(
        ingest_error[0], IndexRejected
    ) else None

    res.check(
        "并发下 ingest 要么被拒、要么未提交（applied=False）",
        rejected is not None or applied is False,
        f"rejected={rejected} applied={applied} error={ingest_error[0] if ingest_error else None}",
    )
    res.check(
        "并发删除后索引未复活（section/chunk 均为 0）",
        counts["section"] == 0 and counts["chunk"] == 0,
        f"counts={counts}",
    )
    res.check(
        "并发后 guard 生命周期为 DELETED",
        state is not None and state.lifecycle_status == LIFECYCLE_DELETED,
        f"lifecycle={state.lifecycle_status if state else None}",
    )


# --------------------------------------------------------------------------- #
# H：V1 pattern 运行时提交 V2
# --------------------------------------------------------------------------- #
def _run_pattern_version_race(res: Results, doc: int) -> None:
    print("\n=== 五、V1 pattern 运行中提交 V2（V1 被丢弃、V2 存在且不重复）===")
    with session_scope() as session:
        _purge_test_tenant(session)

    result = _make_result(doc)
    # 先用 v1 建立 section/chunk 与 guard 基线
    ingest_result(
        result,
        document_id=doc,
        tenant_id=TENANT,
        index_version=1,
        knowledge_base_id=1,
        document_type="historical_bid",
        name="pattern 版本竞争",
        file_name="race.docx",
        file_hash="race1",
        parser="docx",
        with_patterns=False,
    )
    with session_scope() as session:
        id_map = {
            row[0]: row[1]
            for row in session.execute(
                text(
                    "select row_number() over (order by id) - 1 as tmp, id"
                    " from document_section where document_id = :d order by id"
                ),
                {"d": doc},
            ).all()
        }

    indexer = SolutionPatternIndexer()
    pairs = _make_pattern_pairs()

    # --- V1 抢到任务并「开始运行」 ---
    with session_scope() as session:
        owner_v1 = claim_pattern_job(
            session,
            tenant_id=TENANT,
            document_id=doc,
            index_version=1,
            input_hash="race1",
        )
    res.check("V1 抢到 pattern 任务", owner_v1 is not None, f"owner={owner_v1}")

    # --- V1 运行期间，V2 提交 ---
    with session_scope() as session:
        _, decision = begin_index(
            session,
            tenant_id=TENANT,
            document_id=doc,
            index_version=2,
            input_hash="race2",
            require_existing=True,
        )
        assert decision is IndexDecision.APPLY, decision
        commit_index(session, document_id=doc, index_version=2, input_hash="race2")

    # --- V1 跑完，尝试写入 → 必须被丢弃 ---
    with session_scope() as session:
        written_v1 = indexer.save(
            pairs,
            id_map=id_map,
            document_id=doc,
            knowledge_base_id=1,
            tenant_id=TENANT,
            index_version=1,
            owner_id=owner_v1,
            session=session,
        )
    with session_scope() as session:
        current_v1 = pattern_job_is_current(
            session,
            tenant_id=TENANT,
            document_id=doc,
            index_version=1,
            owner_id=owner_v1,  # type: ignore[arg-type]
        )

    res.check(
        "V1 任务在 V2 提交后写入 0 条（旧版本 pattern 被丢弃）",
        written_v1 == 0,
        f"written={written_v1}",
    )
    res.check("V1 任务已不是当前任务", current_v1 is False)

    # --- V2 抢任务并写入 ---
    with session_scope() as session:
        owner_v2 = claim_pattern_job(
            session,
            tenant_id=TENANT,
            document_id=doc,
            index_version=2,
            input_hash="race2",
        )
    res.check("V2 抢到 pattern 任务（V1 不阻塞 V2）", owner_v2 is not None, f"owner={owner_v2}")

    with session_scope() as session:
        written_v2 = indexer.save(
            pairs,
            id_map=id_map,
            document_id=doc,
            knowledge_base_id=1,
            tenant_id=TENANT,
            index_version=2,
            owner_id=owner_v2,
            session=session,
        )
    with session_scope() as session:
        v2_total = session.execute(
            text("select count(*) from solution_pattern where tenant_id = :t"), {"t": TENANT}
        ).scalar()
        job_state = session.execute(
            text(
                "select state from document_pattern_job"
                " where tenant_id = :t and document_id = :d and index_version = 2"
            ),
            {"t": TENANT, "d": doc},
        ).scalar()
        v2_src_versions = session.execute(
            text(
                "select distinct index_version from pattern_source where document_id = :d"
            ),
            {"d": doc},
        ).scalars().all()

    res.check(
        "V2 写入成功且数量正确",
        written_v2 == len(pairs),
        f"written={written_v2} 期望={len(pairs)}",
    )
    res.check(
        "pattern 写入 + job COMPLETED 同事务生效",
        job_state == JOB_COMPLETED,
        f"job.state={job_state}",
    )
    res.check(
        "pattern_source.index_version 落的是 V2",
        list(v2_src_versions) == [2],
        f"versions={list(v2_src_versions)}",
    )

    # --- V2 重复写入必须不产生重复 ---
    with session_scope() as session:
        owner_v2_again = claim_pattern_job(
            session,
            tenant_id=TENANT,
            document_id=doc,
            index_version=2,
            input_hash="race2",
        )
    with session_scope() as session:
        written_again = indexer.save(
            pairs,
            id_map=id_map,
            document_id=doc,
            knowledge_base_id=1,
            tenant_id=TENANT,
            index_version=2,
            owner_id=uuid4(),
            session=session,
        )
    with session_scope() as session:
        v2_total_after = session.execute(
            text("select count(*) from solution_pattern where tenant_id = :t"), {"t": TENANT}
        ).scalar()

    res.check(
        "已 COMPLETED 的任务不会被重复抢占",
        owner_v2_again is None,
        f"owner={owner_v2_again}",
    )
    res.check(
        "非持有者写入被拒（0 条）",
        written_again == 0,
        f"written={written_again}",
    )
    res.check(
        "V2 pattern 不重复",
        v2_total == v2_total_after == len(pairs),
        f"total_before={v2_total} total_after={v2_total_after} 期望={len(pairs)}",
    )

    # --- 删除时把存活任务标 OBSOLETE ---
    with session_scope() as session:
        claim_pattern_job(
            session,
            tenant_id=TENANT,
            document_id=doc,
            index_version=3,
            input_hash="race3",
        )
    delete_document_index(doc, tenant_id=TENANT)
    with session_scope() as session:
        obsolete = session.execute(
            text(
                "select count(*) from document_pattern_job"
                " where tenant_id = :t and document_id = :d and state = :s"
            ),
            {"t": TENANT, "d": doc, "s": JOB_OBSOLETE},
        ).scalar()
    res.check(
        "删除文档时存活 pattern 任务被标 OBSOLETE",
        (obsolete or 0) >= 1,
        f"obsolete={obsolete}",
    )


def _make_pattern_pairs() -> list[tuple[SolutionPatternPayload, Section]]:
    out: list[tuple[SolutionPatternPayload, Section]] = []
    for i, title in enumerate(["质量保证体系", "进度控制措施"], start=1):
        payload = SolutionPatternPayload(
            code=f"SP{i:03d}",
            name=f"通用化组件{i}",
            type="methodology",
            industry="审计服务",
            scenario="政府审计",
            summary=f"通用化摘要{i}",
            structure=["模块A", "模块B"],
            content=f"{title} 的完整正文（含具体事实）。",
            generalized_content=f"{title} 的去事实化正文。",
        )
        sec = Section(title=title, level=1, id=i - 1)
        out.append((payload, sec))
    return out


# --------------------------------------------------------------------------- #
# I：真实 PostgreSQL 特性
# --------------------------------------------------------------------------- #
def _run_pg_feature_checks(res: Results, doc: int) -> None:
    print("\n=== 六、真实 PostgreSQL 特性（版本锁 / partial index / UUID / 回滚）===")

    # --- I1 行锁：第二个会话必须阻塞 ---
    holder = get_session()
    waiter_blocked = threading.Event()
    waiter_done = threading.Event()
    waiter_error: list[BaseException] = []

    def _waiter() -> None:
        session = get_session()
        try:
            session.execute(text("set statement_timeout = '10s'"))
            waiter_blocked.set()
            lock_guard(session, document_id=doc)  # 会阻塞在 FOR UPDATE 上
            session.commit()
        except BaseException as exc:  # noqa: BLE001
            waiter_error.append(exc)
        finally:
            session.close()
            waiter_done.set()

    try:
        with session_scope() as session:
            ensure_guard(session, document_id=doc, tenant_id=TENANT)
        # holder 先持锁不提交（SELECT ... FOR UPDATE 会隐式开启事务）
        holder.execute(
            text("select * from document_index_guard where document_id = :d for update"),
            {"d": doc},
        )

        thread = threading.Thread(target=_waiter, daemon=True)
        thread.start()
        waiter_blocked.wait(timeout=10)
        blocked = not waiter_done.wait(timeout=3)  # 3s 内没结束 → 确实被行锁阻塞
        holder.rollback()
        thread.join(timeout=20)
    finally:
        holder.close()

    res.check(
        "SELECT ... FOR UPDATE 真实阻塞并发会话（版本锁生效）",
        blocked and not waiter_error,
        f"blocked={blocked} waiter_error={waiter_error}",
    )

    # --- I2 partial index ---
    with session_scope() as session:
        idxdefs = {
            row[0]: row[1]
            for row in session.execute(
                text(
                    "select indexname, indexdef from pg_indexes"
                    " where tablename in ('document_pattern_job', 'document_index_guard')"
                )
            ).all()
        }
    live_def = idxdefs.get("idx_document_pattern_job_live", "")
    res.check(
        "partial index idx_document_pattern_job_live 带 WHERE 谓词",
        "WHERE" in live_def.upper() and "RUNNING" in live_def,
        f"{live_def[:120]}",
    )

    # --- I3 UUID 列 ---
    with session_scope() as session:
        col_type = session.execute(
            text(
                "select data_type from information_schema.columns"
                " where table_name = 'document_pattern_job' and column_name = 'owner_id'"
            )
        ).scalar()
        pk_cols = session.execute(
            text(
                "select string_agg(a.attname, ',' order by a.attnum)"
                " from pg_index i"
                " join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)"
                " where i.indrelid = 'document_pattern_job'::regclass and i.indisprimary"
            )
        ).scalar()
    res.check(
        "document_pattern_job.owner_id 为 uuid 类型",
        col_type == "uuid",
        f"data_type={col_type}",
    )
    res.check(
        "document_pattern_job 主键为 (tenant_id, document_id, index_version)",
        pk_cols == "tenant_id,document_id,index_version",
        f"pk={pk_cols}",
    )

    # --- I4 事务回滚 ---
    with session_scope() as session:
        _purge_test_tenant(session)
    with session_scope() as session:
        begin_index(
            session,
            tenant_id=TENANT,
            document_id=doc,
            index_version=1,
            input_hash="rollback",
        )

    class _Boom(RuntimeError):
        pass

    try:
        with session_scope() as session:
            begin_index(
                session,
                tenant_id=TENANT,
                document_id=doc,
                index_version=1,
                input_hash="rollback",
                require_existing=True,
            )
            session.add(
                DocumentChunk(
                    tenant_id=TENANT,
                    document_id=doc,
                    chunk_index=0,
                    chunk_type="paragraph",
                    content="回滚测试 chunk",
                    index_version=1,
                )
            )
            commit_index(session, document_id=doc, index_version=1, input_hash="rollback")
            raise _Boom("模拟落库后失败")
    except _Boom:
        pass

    with session_scope() as session:
        chunk_left = session.execute(
            text("select count(*) from document_chunk where document_id = :d"), {"d": doc}
        ).scalar()
        state = read_guard(session, document_id=doc)
    res.check(
        "事务回滚后 chunk 未落库",
        (chunk_left or 0) == 0,
        f"chunk_left={chunk_left}",
    )
    res.check(
        "事务回滚后版本未被推进（数据与版本一起回退）",
        state is not None and state.current_index_version is None,
        f"current={state.current_index_version if state else None}",
    )

    # --- I5 CHECK 约束 ---
    check_ok = False
    try:
        with session_scope() as session:
            session.execute(
                text(
                    "insert into document_index_guard"
                    " (document_id, tenant_id, lifecycle_status)"
                    " values (:d, :t, 'BOGUS')"
                ),
                {"d": DOC_SEQ_START + 999, "t": TENANT},
            )
    except Exception:  # noqa: BLE001
        check_ok = True
    res.check("guard.lifecycle_status 的 CHECK 约束真实生效", check_ok)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="索引生命周期真实 PG 验收")
    parser.add_argument("--keep-data", action="store_true", help="保留测试数据（排查用）")
    args = parser.parse_args()

    setup_logging()
    _patch_embedding()
    res = Results()

    print("=" * 78)
    print(f"批次 A §4.6 验收：索引生命周期（真实 PostgreSQL，测试租户={TENANT}）")
    print("=" * 78)

    with session_scope() as session:
        _purge_test_tenant(session)
    print("\n=== 零、测试租户已清空 ===")

    try:
        doc = _run_version_matrix(res)
        _run_idempotent_ingest(res, doc)
        _run_delete_tombstone(res, doc)
        _run_concurrent_ingest_delete(res, doc)
        _run_pattern_version_race(res, doc)
        _run_pg_feature_checks(res, doc)
    except Exception:  # noqa: BLE001
        print("\n[ABORT] 验收过程异常：")
        traceback.print_exc()
        res.check("验收脚本无异常", False, "见上方 traceback")
    finally:
        if args.keep_data:
            print("\n[INFO] --keep-data：保留测试数据，未清理。")
        else:
            with session_scope() as session:
                _purge_test_tenant(session)
            print("\n=== 七、清理完成 ===")
            with session_scope() as session:
                left = {
                    "document": session.execute(
                        text("select count(*) from document where tenant_id = :t"), {"t": TENANT}
                    ).scalar(),
                    "guard": session.execute(
                        text(
                            "select count(*) from document_index_guard where tenant_id = :t"
                        ),
                        {"t": TENANT},
                    ).scalar(),
                    "chunk": session.execute(
                        text(
                            "select count(*) from document_chunk where document_id >= :d"
                        ),
                        {"d": DOC_SEQ_START},
                    ).scalar(),
                    "job": session.execute(
                        text(
                            "select count(*) from document_pattern_job where tenant_id = :t"
                        ),
                        {"t": TENANT},
                    ).scalar(),
                }
                res.check(
                    "测试租户数据已全部清除",
                    all((v or 0) == 0 for v in left.values()),
                    f"{left}",
                )

    print("\n" + "=" * 78)
    total = len(res.checks)
    failed = res.failed
    if failed:
        print(f"[FAIL] {total - len(failed)}/{total} 通过，{len(failed)} 项失败：")
        for name, _ok, detail in failed:
            print(f"       - {name}  {detail}")
        print("=" * 78)
        return 1
    print(f"[PASS] {total}/{total} 全部通过：索引生命周期在真实 PostgreSQL 上收敛正确。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
