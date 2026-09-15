"""文档入库编排:document 记录 + 章节 + chunk + pattern,全链路关联。

批次 A 起，本模块的所有写入都在**索引生命周期守卫**（`document_index_guard`）之下进行：

1. 短事务：建 guard + `SELECT ... FOR UPDATE` + 版本判定；
2. 事务外：向量化（慢操作，不占锁）；
3. 短事务：**重新校验**版本与 lifecycle → 原子替换 section/chunk → 提交版本。

这样「首次 ingest 与 delete 并发」时，删除写入的墓碑会在第 3 步被看到，晚到的 ingest
不会把索引复活；而「V1 任务在 V2 提交后才跑完」也会因为 guard.current 已推进而丢弃 V1 的 pattern。
"""

from __future__ import annotations

import threading
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import add_vector_extension, init_db, session_scope
from app.core.doc_types import is_pattern_eligible, normalize_document_type
from app.core.logging import get_logger
from app.core.tenant import TenantScopeError, require_tenant_id
from app.knowledge.guard import (
    IndexDecision,
    IndexRejected,
    begin_index,
    claim_pattern_job,
    commit_index,
    compute_index_input_hash,
    obsolete_all_live_jobs,
    pattern_job_is_current,
    purge_document_index,
    read_guard,
    tombstone,
)
from app.knowledge.indexer import KnowledgeIndexer
from app.models.chunk import DocumentChunk
from app.models.document import DocumentRecord
from app.models.pattern import PatternSource, SolutionPattern
from app.models.section import DocumentSection

logger = get_logger(__name__)

# 方案组件后台抽取的并发保护：同一文档同时只跑一个抽取任务。
# **只是优化**，不是正确性依据 —— 正确性由 `document_pattern_job` + guard 版本判定保证。
_extracting_docs: set[int] = set()
_extracting_lock = threading.Lock()


def resolve_index_version(
    *, index_version: int | None, parse_log_id: int | None
) -> int:
    """确定本次索引版本。

    `parseLogId` 是唯一权威来源；若调用方额外传了 `indexVersion`，两者必须相等，否则拒绝
    （避免「日志说第 3 次尝试、数据却按第 5 个版本写」这种无法对账的状态）。
    """
    if parse_log_id is not None and index_version is not None and parse_log_id != index_version:
        raise TenantScopeError(
            f"indexVersion({index_version}) 与 parseLogId({parse_log_id}) 不一致，拒绝写入"
        )
    resolved = index_version if index_version is not None else parse_log_id
    if resolved is None:
        # 兼容历史脚本：没有 parseLogId 时视为第 1 个版本，并留下显式日志便于排查。
        logger.warning("未提供 parseLogId/indexVersion，按 index_version=1 处理")
        return 1
    if resolved <= 0:
        raise TenantScopeError(f"index_version 非法({resolved!r})")
    return resolved


def _ensure_document(
    document_id: int,
    *,
    knowledge_base_id: int | None = None,
    tenant_id: int | None = None,
    enterprise_id: int | None = None,
    name: str | None = None,
    file_name: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    document_type: str | None = None,
    storage_key: str | None = None,
    file_hash: str | None = None,
    parser: str | None = None,
    session: Session | None = None,
) -> None:
    """按 document_id 建/更新 document 记录(幂等 upsert)。"""
    if session is None:
        with session_scope() as db_session:
            _ensure_document(
                document_id,
                knowledge_base_id=knowledge_base_id,
                tenant_id=tenant_id,
                enterprise_id=enterprise_id,
                name=name,
                file_name=file_name,
                file_type=file_type,
                file_size=file_size,
                document_type=document_type,
                storage_key=storage_key,
                file_hash=file_hash,
                parser=parser,
                session=db_session,
            )
        return

    record = session.get(DocumentRecord, document_id)
    if record is None:
        record = DocumentRecord(
            id=document_id,
            tenant_id=tenant_id,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            name=name,
            file_name=file_name or f"doc-{document_id}",
            file_type=file_type or "",
            file_size=file_size,
            storage_key=storage_key,
            document_type=document_type or "historical_bid",
            status="embedded",
            parser=parser,
            file_hash=file_hash,
        )
        session.add(record)
    else:
        # 禁止普通 upsert 改写归属:document 已属于某租户时,别的租户不得覆盖它。
        if tenant_id is not None:
            if record.tenant_id is not None and record.tenant_id != tenant_id:
                raise TenantScopeError(
                    f"document {document_id} 属于租户 {record.tenant_id}，拒绝被租户 {tenant_id} 覆盖"
                )
            record.tenant_id = tenant_id
        record.enterprise_id = enterprise_id
        record.knowledge_base_id = knowledge_base_id
        record.name = name or record.name
        record.file_name = file_name or record.file_name
        record.file_type = file_type or record.file_type
        record.file_size = file_size
        record.storage_key = storage_key
        record.document_type = document_type or record.document_type
        record.parser = parser
        record.file_hash = file_hash
        record.status = "embedded"
    logger.info("document 记录就绪: id=%s", document_id)


def ingest_result(
    result: Any,
    *,
    document_id: int,
    tenant_id: int,
    index_version: int | None = None,
    parse_log_id: int | None = None,
    input_hash: str | None = None,
    knowledge_base_id: int | None = None,
    enterprise_id: int | None = None,
    document_type: str = "historical_bid",
    name: str | None = None,
    file_name: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    storage_key: str | None = None,
    file_hash: str | None = None,
    parser: str | None = None,
    with_patterns: bool = True,
) -> dict[str, Any]:
    """一次入库:document 记录 + 章节 + chunk,可选后台抽取方案组件。

    返回 {"document_id", "index_version", "applied", "section_count", "chunk_count", "pattern_count"}。

    applied=False 表示本次没有写入（同版本同输入的重试，幂等跳过）。
    被守卫拒绝时抛 `IndexRejected`，由 API 层映射为 409/410。
    """
    # 落库必须有租户边界:没有租户的索引既无法被安全检索,也无法归属。
    tenant_id = require_tenant_id(tenant_id, where="ingest_result")
    version = resolve_index_version(index_version=index_version, parse_log_id=parse_log_id)
    # 文档类型归一化后再用:脏值（空串/未知类型）不进索引，避免后续类型过滤失效。
    document_type = normalize_document_type(document_type)
    # §4.4：tender（招标文件）是「本次项目约束」，不是可复用知识，不得产生方案组件。
    # 这里做**持久化层的强制收口**：即使调用方传了 with_patterns=True 也会被改掉，
    # 不能只依赖 Java 侧自觉传 false。
    if with_patterns and not is_pattern_eligible(document_type):
        logger.info(
            "文档 %s 类型为 %s，不属于方案组件可用类型，强制跳过 pattern 抽取",
            document_id,
            document_type,
        )
        with_patterns = False
    digest = input_hash or compute_index_input_hash(
        file_hash=file_hash,
        document_type=document_type,
        knowledge_base_id=knowledge_base_id,
        parser=parser,
        chunk_max_chars=settings.chunk_max_chars,
        chunk_overlap=settings.chunk_overlap,
    )

    # 初始化结构在事务外执行；真正替换在短事务内完成，失败时保留旧索引。
    add_vector_extension()
    init_db()

    indexer = KnowledgeIndexer()

    # --- 阶段 1：短事务建 guard + 判定版本 ---
    with session_scope() as session:
        state, decision = begin_index(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            index_version=version,
            input_hash=digest,
        )
    _raise_if_rejected(decision, document_id=document_id, index_version=version)
    if decision is IndexDecision.IDEMPOTENT:
        logger.info(
            "文档 %s 版本 %s 同输入已提交，幂等跳过（不重建 section/chunk）",
            document_id,
            version,
        )
        return {
            "document_id": document_id,
            "index_version": version,
            "applied": False,
            "section_count": 0,
            "chunk_count": 0,
            "pattern_count": 0,
        }

    # --- 阶段 2：事务外向量化（慢操作，不持有 guard 行锁） ---
    vectors = indexer.embed_chunks(result.chunks)

    # --- 阶段 3：短事务重新校验 + 原子替换 + 提交版本 ---
    with session_scope() as session:
        _, decision = begin_index(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            index_version=version,
            input_hash=digest,
            require_existing=True,
        )
        if decision is IndexDecision.IDEMPOTENT:
            # 期间有并发同版本同输入提交成功，本次无需再写。
            return {
                "document_id": document_id,
                "index_version": version,
                "applied": False,
                "section_count": 0,
                "chunk_count": 0,
                "pattern_count": 0,
            }
        _raise_if_rejected(decision, document_id=document_id, index_version=version)

        _replace_index(
            session,
            indexer=indexer,
            result=result,
            document_id=document_id,
            tenant_id=tenant_id,
            version=version,
            vectors=vectors,
            knowledge_base_id=knowledge_base_id,
            enterprise_id=enterprise_id,
            document_type=document_type,
            name=name,
            file_name=file_name,
            file_type=file_type,
            file_size=file_size,
            storage_key=storage_key,
            file_hash=file_hash,
            parser=parser,
        )
        stats = session.info["ingest_stats"]

        # 版本提交必须与数据替换同事务：否则「数据已换、版本没动」会让后续
        # 同版本重试被误判为「同版本不同输入」而拒绝。
        commit_index(
            session, document_id=document_id, index_version=version, input_hash=digest
        )

    # 方案组件抽取需**逐章节调用 LLM**：一篇 150+ 章节的标书要跑二十来次 LLM 请求，
    # 实测串行耗时 6 分钟以上，远超调用方（Java 侧 bidox.ai.read-timeout=300s）。
    # 因此主事务（document/章节/chunk）提交后，把抽取放到后台线程执行，
    # 让同步解析接口快速返回；pattern_count 在本次响应中恒为 0（数据仍会落库）。
    # 注：抽取内部已用有界线程池并发（PATTERN_EXTRACT_CONCURRENCY），耗时约为串行的 1/N。
    if with_patterns:
        _spawn_pattern_extraction(
            result,
            stats["id_map"],
            document_id=document_id,
            index_version=version,
            input_hash=digest,
            knowledge_base_id=knowledge_base_id,
            tenant_id=tenant_id,
            enterprise_id=enterprise_id,
            title=name or file_name,
            document_type=document_type,
        )

    return {
        "document_id": document_id,
        "index_version": version,
        "applied": True,
        "section_count": stats["section_count"],
        "chunk_count": stats["chunk_count"],
        "pattern_count": 0,
    }


def _raise_if_rejected(decision: IndexDecision, *, document_id: int, index_version: int) -> None:
    """把判定结果映射成异常（APPLY / IDEMPOTENT 之外一律拒绝）。"""
    if not decision.rejected:
        return
    raise IndexRejected(
        decision,
        f"文档 {document_id} 版本 {index_version} 的索引写入被拒绝：{decision.value}",
    )


def _replace_index(
    session: Session,
    *,
    indexer: KnowledgeIndexer,
    result: Any,
    document_id: int,
    tenant_id: int,
    version: int,
    vectors: list[list[float]],
    knowledge_base_id: int | None,
    enterprise_id: int | None,
    document_type: str,
    name: str | None,
    file_name: str | None,
    file_type: str | None,
    file_size: int | None,
    storage_key: str | None,
    file_hash: str | None,
    parser: str | None,
) -> None:
    """在**已持有 guard 行锁**的事务内做原子替换（先删旧再写新）。"""
    # pattern 的清理按来源进行；pattern 本体只在无其他来源时才删（见 purge 语义）。
    old_pattern_ids = session.execute(
        PatternSource.__table__.select()
        .with_only_columns(PatternSource.pattern_id)
        .where(PatternSource.document_id == document_id)
    ).scalars().all()
    session.execute(delete(PatternSource).where(PatternSource.document_id == document_id))
    if old_pattern_ids:
        still_used = set(
            session.execute(
                select(PatternSource.pattern_id)
                .where(PatternSource.pattern_id.in_(old_pattern_ids))
                .distinct()
            ).scalars()
        )
        orphan = [pid for pid in set(old_pattern_ids) if pid not in still_used]
        if orphan:
            session.execute(delete(SolutionPattern).where(SolutionPattern.id.in_(orphan)))
    session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    session.execute(delete(DocumentSection).where(DocumentSection.document_id == document_id))

    _ensure_document(
        document_id,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id,
        enterprise_id=enterprise_id,
        name=name,
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        document_type=document_type,
        storage_key=storage_key,
        file_hash=file_hash,
        parser=parser,
        session=session,
    )

    stats = indexer.index_document(
        result,
        document_id,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id,
        enterprise_id=enterprise_id,
        index_version=version,
        vectors=vectors,
        session=session,
        prepare_schema=False,
    )
    session.info["ingest_stats"] = stats


def delete_document_index(document_id: int, *, tenant_id: int) -> dict[str, Any]:
    """幂等删除文档索引：写墓碑 + 清理 section/chunk/pattern_source。

    - 重复调用安全：第二次 `already_deleted=True`，不报错。
    - guard 行**保留**（墓碑），所以删除之后任何晚到的 ingest 都会被 `REJECT_DELETED` 拦下，
      索引不会被复活；要恢复必须新建 document_id。
    """
    tenant_id = require_tenant_id(tenant_id, where="delete_document_index")
    add_vector_extension()
    init_db()

    with session_scope() as session:
        first_time = tombstone(session, tenant_id=tenant_id, document_id=document_id)
        purged = purge_document_index(session, document_id=document_id)
        obsoleted = obsolete_all_live_jobs(
            session, tenant_id=tenant_id, document_id=document_id
        )
        state = read_guard(session, document_id=document_id)

    logger.info(
        "文档 %s 索引已删除（首次=%s, purged=%s, obsoleted_jobs=%s）",
        document_id,
        first_time,
        purged,
        obsoleted,
    )
    return {
        "document_id": document_id,
        "deleted": True,
        "already_deleted": not first_time,
        "lifecycle_status": state.lifecycle_status if state else None,
        "purged": purged,
        "obsoleted_jobs": obsoleted,
    }


def _spawn_pattern_extraction(
    result: Any,
    id_map: dict[int, int],
    *,
    document_id: int,
    index_version: int,
    input_hash: str,
    knowledge_base_id: int | None = None,
    tenant_id: int | None = None,
    enterprise_id: int | None = None,
    title: str | None = None,
    document_type: str | None = None,
) -> None:
    """在后台线程中抽取并入库方案组件（逐章节调 LLM，耗时可达数分钟）。

    正确性不依赖内存标记：
    - 先 `claim_pattern_job` 抢 `(tenant, document, index_version)` 的持久化任务与租约；
    - 抽取在事务外跑；
    - 写 pattern 的短事务里再查 `pattern_job_is_current`（guard.current == 本版本 且 job 仍归自己），
      不满足就整批丢弃 —— 这样 V1 任务在 V2 提交后才跑完时不会污染 V2 的索引；
    - `pattern 写入 + job COMPLETED` 同一事务，避免「写了一半 job 还在 RUNNING」导致重复写入。
    """
    if tenant_id is None:
        logger.warning("文档 %s 缺少租户，跳过方案组件抽取", document_id)
        return

    # §4.4 第二道收口：即便绕过 ingest_result 直接调用本函数，也不允许非白名单类型产生 pattern。
    # 放在**抢任务之前**，避免为注定要丢弃的文档占用 pattern job 租约。
    normalized_type = normalize_document_type(document_type)
    if not is_pattern_eligible(normalized_type):
        logger.info(
            "文档 %s 类型为 %s，不属于方案组件可用类型，跳过 pattern 抽取",
            document_id,
            normalized_type,
        )
        return

    # 抢占任务（独立短事务，尽早释放锁）
    owner: UUID | None
    with session_scope() as session:
        owner = claim_pattern_job(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            index_version=index_version,
            input_hash=input_hash,
        )
    if owner is None:
        logger.info(
            "文档 %s 版本 %s 的方案组件任务已完成或已被持有，跳过本次触发",
            document_id,
            index_version,
        )
        return

    def _run() -> None:
        try:
            from app.pattern.indexer import SolutionPatternIndexer

            pattern_indexer = SolutionPatternIndexer()
            pairs = pattern_indexer.extract(
                result, doc_meta={"title": title}, document_type=normalized_type
            )
            if not pairs:
                with session_scope() as session:
                    if pattern_job_is_current(
                        session,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        index_version=index_version,
                        owner_id=owner,
                    ):
                        pattern_indexer.complete_job(
                            session,
                            document_id=document_id,
                            tenant_id=tenant_id,
                            index_version=index_version,
                            owner_id=owner,
                        )
                logger.info("文档 %s 方案组件抽取完成: 0 个", document_id)
                return

            with session_scope() as session:
                count = pattern_indexer.save(
                    pairs,
                    id_map=id_map,
                    document_id=document_id,
                    knowledge_base_id=knowledge_base_id,
                    tenant_id=tenant_id,
                    enterprise_id=enterprise_id,
                    index_version=index_version,
                    owner_id=owner,
                    session=session,
                    document_type=normalized_type,
                )
            logger.info(
                "文档 %s 方案组件后台抽取完成: %s 个（index_version=%s）",
                document_id,
                count,
                index_version,
            )
        except Exception:  # noqa: BLE001 - 后台线程不能抛出，否则异常会被静默丢弃
            logger.exception("文档 %s 方案组件后台抽取失败", document_id)
            try:
                with session_scope() as session:
                    from app.knowledge.guard import fail_pattern_job

                    fail_pattern_job(
                        session,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        index_version=index_version,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("文档 %s 标记方案组件任务失败时出错", document_id)
        finally:
            with _extracting_lock:
                _extracting_docs.discard(document_id)

    with _extracting_lock:
        if document_id in _extracting_docs:
            logger.info("文档 %s 方案组件抽取已在进行中，跳过本次触发", document_id)
            return
        _extracting_docs.add(document_id)

    threading.Thread(
        target=_run,
        name=f"pattern-extract-{document_id}",
        daemon=True,
    ).start()
