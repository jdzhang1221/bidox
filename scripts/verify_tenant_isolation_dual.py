"""批次 A §4.6 验收：双租户同文本 / 同 fingerprint 零串库（真实库端到端）。

验收原文（开发计划 §4.6）::

    双租户同文本、同 fingerprint 下，所有检索/扩展路径零串库。

为什么单测不够
--------------
`tests/test_tenant_isolation.py` 用假 session 断言 SQL 里带没带 `tenant_id = ?`，
证明的是「构造正确」；本脚本证明的是「真实库 + 真实 pgvector + 真实 SQLAlchemy 下，
同文本同向量的干扰数据确实捞不出来」。

做法
----
1. 从租户 1 挑一份「有 chunk（带 embedding）+ section + pattern」的文档当模板；
2. 为租户 2 造一份**同构镜像**：同文本、同 embedding、同 fingerprint、**同知识库**，
   仅 `tenant_id` 不同 —— 干扰强度拉满：任何一处漏加租户条件，镜像都会以距离 0 排到第一；
3. 逐条跑检索/扩展路径，断言租户 1 的结果里**绝不出现**镜像行；
   同时断言租户 2 能召回镜像（**反证**：证明镜像真的可召回，测试不是空转）；
4. 清理镜像，复核一致性不变量仍全 0、各表行数复原。

用法::

    uv run python scripts/verify_tenant_isolation_dual.py
    uv run python scripts/verify_tenant_isolation_dual.py --keep-mirror   # 排查用，保留镜像数据

退出码：0 = 全部通过；1 = 存在失败项。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text

from app.core.database import session_scope
from app.core.logging import get_logger, setup_logging
from app.knowledge.service import KnowledgeService
from app.models.chunk import DocumentChunk
from app.models.document import DocumentRecord
from app.models.index_guard import LIFECYCLE_ACTIVE, DocumentIndexGuard
from app.models.pattern import PatternSource, SolutionPattern
from app.models.section import DocumentSection
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.keyword import KeywordRetriever
from app.retrieval.pattern import PatternRetriever
from app.retrieval.section import SectionRetriever
from app.retrieval.vector import VectorRetriever

logger = get_logger(__name__)

# --- 固定哨兵：让脚本可重复执行（每次先清残留，再重建） ---
TENANT_SRC = 1
TENANT_MIRROR = 2
MIRROR_DOC_ID = 9_000_001
MIRROR_DOC_NAME = "[MIRROR-TENANT-2] 租户隔离验收镜像"
MIRROR_PATTERN_CODE = "MIRROR-T2"
MAX_MIRROR_CHUNKS = 60  # 镜像 chunk 上限，避免超大文档拖慢验收

# 镜像里挑一个「有父章节也有兄弟章节」的章节来验兄弟扩展
# （见 _pick_probe_section）


# --------------------------------------------------------------------------- #
# 断言结果收集
# --------------------------------------------------------------------------- #
@dataclass
class Results:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name}")
        if detail:
            print(f"         {detail}")
        return bool(ok)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]


# --------------------------------------------------------------------------- #
# 镜像数据构造 / 清理
# --------------------------------------------------------------------------- #
def _purge_mirror(session) -> None:
    """按哨兵清除镜像数据（幂等，供开跑前清残留与收尾共用）。

    哨兵只有两个：`document.id = MIRROR_DOC_ID` 与 `solution_pattern.code = MIRROR_PATTERN_CODE`。
    即使上一轮跑到一半崩掉，这两处也一定能定位到残留。
    """
    session.execute(
        text("delete from pattern_source where document_id = :doc"), {"doc": MIRROR_DOC_ID}
    )
    session.execute(
        text(
            "delete from pattern_source where pattern_id in"
            " (select id from solution_pattern where code = :code)"
        ),
        {"code": MIRROR_PATTERN_CODE},
    )
    session.execute(
        text("delete from solution_pattern where code = :code"), {"code": MIRROR_PATTERN_CODE}
    )
    session.execute(
        text("delete from document_chunk where document_id = :doc"), {"doc": MIRROR_DOC_ID}
    )
    session.execute(
        text("delete from document_section where document_id = :doc"), {"doc": MIRROR_DOC_ID}
    )
    session.execute(
        text("delete from document_pattern_job where document_id = :doc"), {"doc": MIRROR_DOC_ID}
    )
    session.execute(
        text("delete from document_index_guard where document_id = :doc"), {"doc": MIRROR_DOC_ID}
    )
    session.execute(text("delete from document where id = :doc"), {"doc": MIRROR_DOC_ID})


def _snapshot_counts(session) -> dict[str, int]:
    """各表行数快照，用于验证清理是否复原。"""
    tables = [
        "document",
        "document_chunk",
        "document_section",
        "solution_pattern",
        "pattern_source",
        "document_index_guard",
        "document_pattern_job",
    ]
    return {t: session.execute(text(f"select count(*) from {t}")).scalar() or 0 for t in tables}


@dataclass
class Mirror:
    doc_id: int
    kb_id: int | None
    section_ids: list[int]
    chunk_ids: list[int]
    pattern_id: int
    pattern_fp: str
    query: str
    probe_section_id: int  # 镜像侧「有父有兄弟」的章节
    src_section_id: int  # 对应的租户 1 章节
    src_doc_id: int
    src_chunk_id: int
    src_pattern_id: int  # 租户 1 侧同 fingerprint 的源 pattern


def _pick_source_template(session) -> tuple[int, int, int]:
    """挑模板：返回 (document_id, chunk_id, pattern_id)。

    要求：租户 1、有带 embedding 的 chunk、有 section、有带 fingerprint 的 pattern。
    """
    doc_id = session.execute(
        text(
            """
            select d.id
            from document d
            where d.tenant_id = :t
              and exists (select 1 from document_chunk c
                          where c.document_id = d.id and c.embedding is not null)
              and exists (select 1 from document_section s where s.document_id = d.id)
              and exists (
                    select 1 from pattern_source ps
                    join solution_pattern p on p.id = ps.pattern_id
                    where ps.document_id = d.id
                      and p.fingerprint is not null
                      and p.tenant_id = :t)
            order by (select count(*) from document_chunk c
                      where c.document_id = d.id and c.embedding is not null) desc,
                     d.id asc
            limit 1
            """
        ),
        {"t": TENANT_SRC},
    ).scalar()
    if doc_id is None:
        raise SystemExit(
            "[ABORT] 库里找不到满足条件的租户 1 模板文档"
            "（需要同时有 embedding chunk / section / 带 fingerprint 的 pattern）。"
        )

    chunk_id = session.execute(
        text(
            "select id from document_chunk where document_id = :d and embedding is not null"
            " order by chunk_index asc, id asc limit 1"
        ),
        {"d": doc_id},
    ).scalar()

    pattern_id = session.execute(
        text(
            """
            select ps.pattern_id
            from pattern_source ps
            join solution_pattern p on p.id = ps.pattern_id
            where ps.document_id = :d and p.fingerprint is not null and p.tenant_id = :t
            order by ps.sort_order asc, ps.id asc
            limit 1
            """
        ),
        {"d": doc_id, "t": TENANT_SRC},
    ).scalar()

    if chunk_id is None or pattern_id is None:
        raise SystemExit("[ABORT] 模板文档缺少可用 chunk 或 pattern，无法构造镜像。")
    return int(doc_id), int(chunk_id), int(pattern_id)


def _build_mirror(session) -> Mirror:
    src_doc_id, src_chunk_id, src_pattern_id = _pick_source_template(session)

    src_doc: DocumentRecord = session.get(DocumentRecord, src_doc_id)
    src_chunk: DocumentChunk = session.get(DocumentChunk, src_chunk_id)
    src_pattern: SolutionPattern = session.get(SolutionPattern, src_pattern_id)

    # --- 1. 镜像 document：同知识库、同类型，仅 tenant 不同 ---
    mirror_doc = DocumentRecord(
        id=MIRROR_DOC_ID,
        tenant_id=TENANT_MIRROR,
        enterprise_id=src_doc.enterprise_id,
        project_id=src_doc.project_id,
        knowledge_base_id=src_doc.knowledge_base_id,
        name=MIRROR_DOC_NAME,
        file_name=src_doc.file_name,
        file_type=src_doc.file_type,
        file_size=src_doc.file_size,
        storage_key=src_doc.storage_key,
        document_type=src_doc.document_type,
        status=src_doc.status,
        parser=src_doc.parser,
        parser_version=src_doc.parser_version,
        page_count=src_doc.page_count,
    )
    session.add(mirror_doc)

    # --- 2. 镜像 section 树：保持父子结构同构（parent_id 重映射） ---
    src_sections = (
        session.execute(
            select(DocumentSection)
            .where(DocumentSection.document_id == src_doc_id)
            .order_by(DocumentSection.sort_order.asc(), DocumentSection.id.asc())
        )
        .scalars()
        .all()
    )
    sec_map: dict[int, int] = {}
    # 先建不带 parent 的行拿到自增 id，再回填 parent_id
    for s in src_sections:
        row = DocumentSection(
            document_id=MIRROR_DOC_ID,
            knowledge_base_id=src_doc.knowledge_base_id,
            parent_id=None,
            title=s.title,
            level=s.level,
            section_no=s.section_no,
            section_type=s.section_type,
            sort_order=s.sort_order,
            page_start=s.page_start,
            page_end=s.page_end,
            content=s.content,
            meta=s.meta,
        )
        session.add(row)
        session.flush()
        sec_map[s.id] = row.id
    for s in src_sections:
        if s.parent_id and s.parent_id in sec_map:
            session.get(DocumentSection, sec_map[s.id]).parent_id = sec_map[s.parent_id]
    session.flush()

    # --- 3. 镜像 chunk：同文本、**同 embedding**（干扰强度最大化） ---
    src_chunks = (
        session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == src_doc_id, DocumentChunk.embedding.is_not(None))
            .order_by(DocumentChunk.chunk_index.asc(), DocumentChunk.id.asc())
            .limit(MAX_MIRROR_CHUNKS)
        )
        .scalars()
        .all()
    )
    chunk_map: dict[int, int] = {}
    for c in src_chunks:
        row = DocumentChunk(
            tenant_id=TENANT_MIRROR,
            enterprise_id=src_doc.enterprise_id,
            knowledge_base_id=src_doc.knowledge_base_id,
            document_id=MIRROR_DOC_ID,
            section_id=sec_map.get(c.section_id) if c.section_id else None,
            chunk_index=c.chunk_index,
            chunk_type=c.chunk_type,
            title=c.title,
            content=c.content,
            document_type=c.document_type,
            section_type=c.section_type,
            page_start=c.page_start,
            page_end=c.page_end,
            meta=c.meta,
            embedding=c.embedding,
        )
        session.add(row)
        session.flush()
        chunk_map[c.id] = row.id

    # --- 4. 镜像 pattern：同 fingerprint（跨租户去重泄漏的关键场景） ---
    mirror_pattern = SolutionPattern(
        tenant_id=TENANT_MIRROR,
        enterprise_id=src_pattern.enterprise_id,
        knowledge_base_id=src_doc.knowledge_base_id,
        name=src_pattern.name,
        code=MIRROR_PATTERN_CODE,
        type=src_pattern.type,
        industry=src_pattern.industry,
        scenario=src_pattern.scenario,
        summary=src_pattern.summary,
        structure=src_pattern.structure,
        content=src_pattern.content,
        generalized_content=src_pattern.generalized_content,
        fingerprint=src_pattern.fingerprint,
        version=src_pattern.version,
        status=src_pattern.status,
        embedding=src_pattern.embedding,
    )
    session.add(mirror_pattern)
    session.flush()

    # --- 5. 镜像 pattern_source：指向镜像 doc/section/chunk ---
    src_sources = (
        session.execute(
            select(PatternSource)
            .where(PatternSource.pattern_id == src_pattern_id)
            .order_by(PatternSource.sort_order.asc(), PatternSource.id.asc())
        )
        .scalars()
        .all()
    )
    for ps in src_sources:
        session.add(
            PatternSource(
                pattern_id=mirror_pattern.id,
                document_id=MIRROR_DOC_ID if ps.document_id else None,
                section_id=sec_map.get(ps.section_id) if ps.section_id else None,
                chunk_id=chunk_map.get(ps.chunk_id) if ps.chunk_id else None,
                source_type=ps.source_type,
                page_start=ps.page_start,
                page_end=ps.page_end,
                sort_order=ps.sort_order,
            )
        )
    session.flush()

    # --- 6. 镜像 guard：检索层有「生命周期必须 ACTIVE」的硬边界，
    #        镜像若无 guard 行就检不到，反证会变成空转。
    session.add(
        DocumentIndexGuard(
            document_id=MIRROR_DOC_ID,
            tenant_id=TENANT_MIRROR,
            lifecycle_status=LIFECYCLE_ACTIVE,
            current_index_version=1,
            index_input_hash=None,
        )
    )
    session.flush()

    # --- 7. 挑一个「镜像侧有父且有兄弟」的章节，作为跨租户扩展探针 ---
    probe = None
    for s in src_sections:
        if not s.parent_id or s.parent_id not in sec_map:
            continue
        sibling_count = sum(1 for o in src_sections if o.parent_id == s.parent_id)
        if sibling_count >= 2:
            probe = s
            break
    if probe is None:  # 退化：只要该文档有子章节就够验「父章节」
        for s in src_sections:
            if s.parent_id and s.parent_id in sec_map:
                probe = s
                break
    if probe is None:
        probe = src_sections[0]

    return Mirror(
        doc_id=MIRROR_DOC_ID,
        kb_id=src_doc.knowledge_base_id,
        section_ids=list(sec_map.values()),
        chunk_ids=list(chunk_map.values()),
        pattern_id=mirror_pattern.id,
        pattern_fp=src_pattern.fingerprint or "",
        query=(src_chunk.content or "")[:60],
        probe_section_id=sec_map[probe.id],
        src_section_id=probe.id,
        src_doc_id=src_doc_id,
        src_chunk_id=src_chunk_id,
        src_pattern_id=src_pattern_id,
    )


# --------------------------------------------------------------------------- #
# 断言辅助
# --------------------------------------------------------------------------- #
def _ids(rows: list[dict[str, Any]], key: str) -> list[Any]:
    return [r.get(key) for r in rows]


def _leak(rows: list[dict[str, Any]], mirror: Mirror) -> list[dict[str, Any]]:
    """从结果里挑出属于镜像的行。"""
    return [r for r in rows if r.get("document_id") == mirror.doc_id]


# --------------------------------------------------------------------------- #
# 各路径验收
# --------------------------------------------------------------------------- #
def _run_retrieval_checks(res: Results, mirror: Mirror) -> None:
    print("\n=== 一、检索通道：租户 1 不得见镜像 / 租户 2 必须见镜像（反证）===")

    # 用模板 chunk 自己的向量做查询：镜像 chunk 距离恒为 0，漏租户条件必然排第一
    with session_scope() as session:
        qvec = session.get(DocumentChunk, mirror.src_chunk_id).embedding
        qvec = list(qvec) if qvec is not None else None
    if qvec is None:
        raise SystemExit("[ABORT] 模板 chunk 没有 embedding，无法做向量验收。")

    vec = VectorRetriever()
    kw = KeywordRetriever()
    pat = PatternRetriever()

    # --- 向量检索 ---
    r1 = vec.search(qvec, tenant_id=TENANT_SRC, top_k=50)
    res.check(
        "VectorRetriever(tenant=1) 无镜像 chunk",
        not _leak(r1, mirror),
        f"命中 {len(r1)} 条，其中镜像 {len(_leak(r1, mirror))} 条",
    )
    r2 = vec.search(qvec, tenant_id=TENANT_MIRROR, top_k=50)
    res.check(
        "VectorRetriever(tenant=2) 能召回镜像 chunk（反证：镜像真实存在且可召回）",
        bool(_leak(r2, mirror)),
        f"命中 {len(r2)} 条，其中镜像 {len(_leak(r2, mirror))} 条",
    )

    # --- 关键词检索 ---
    k1 = kw.search(mirror.query, tenant_id=TENANT_SRC, top_k=50)
    res.check(
        "KeywordRetriever(tenant=1) 无镜像 chunk",
        not _leak(k1, mirror),
        f"命中 {len(k1)} 条，其中镜像 {len(_leak(k1, mirror))} 条",
    )
    k2 = kw.search(mirror.query, tenant_id=TENANT_MIRROR, top_k=50)
    res.check(
        "KeywordRetriever(tenant=2) 能召回镜像 chunk（反证）",
        bool(_leak(k2, mirror)),
        f"命中 {len(k2)} 条，其中镜像 {len(_leak(k2, mirror))} 条",
    )

    # --- 方案组件检索 ---
    p1 = pat.search(qvec, tenant_id=TENANT_SRC, top_k=50, status=None)
    res.check(
        "PatternRetriever(tenant=1) 无镜像 pattern",
        all(x.get("pattern_id") != mirror.pattern_id for x in p1),
        f"命中 {len(p1)} 条，含镜像 pattern={any(x.get('pattern_id') == mirror.pattern_id for x in p1)}",
    )
    p2 = pat.search(qvec, tenant_id=TENANT_MIRROR, top_k=50, status=None)
    res.check(
        "PatternRetriever(tenant=2) 能召回镜像 pattern（反证）",
        any(x.get("pattern_id") == mirror.pattern_id for x in p2),
        f"命中 {len(p2)} 条，pattern_ids={_ids(p2, 'pattern_id')[:8]}",
    )

    # --- 混合检索（双通道） ---
    hy = HybridRetriever()
    h1 = hy.retrieve(mirror.query, tenant_id=TENANT_SRC, top_k=50, pattern_top_k=50, rerank=False)
    res.check(
        "HybridRetriever.retrieve(tenant=1) evidences 无镜像",
        not _leak(h1["evidences"], mirror),
        f"evidences={len(h1['evidences'])}，镜像={len(_leak(h1['evidences'], mirror))}",
    )
    res.check(
        "HybridRetriever.retrieve(tenant=1) patterns 无镜像",
        all(x.get("pattern_id") != mirror.pattern_id for x in h1["patterns"]),
        f"patterns={len(h1['patterns'])}",
    )
    h2 = hy.retrieve(mirror.query, tenant_id=TENANT_MIRROR, top_k=50, pattern_top_k=50, rerank=False)
    res.check(
        "HybridRetriever.retrieve(tenant=2) 能召回镜像（反证）",
        bool(_leak(h2["evidences"], mirror))
        or any(x.get("pattern_id") == mirror.pattern_id for x in h2["patterns"]),
        f"evidences={len(h2['evidences'])} patterns={len(h2['patterns'])}",
    )

    # --- 不带知识库条件（「不选知识库 = 跨本租户」）也必须零串库 ---
    n1 = vec.search(qvec, tenant_id=TENANT_SRC, top_k=50, knowledge_base_id=None)
    res.check(
        "不选知识库时（跨本租户）向量检索仍无镜像",
        not _leak(n1, mirror),
        f"命中 {len(n1)} 条，镜像 {len(_leak(n1, mirror))} 条",
    )
    b1 = vec.search(qvec, tenant_id=TENANT_SRC, top_k=50, knowledge_base_id=mirror.kb_id)
    res.check(
        "显式指定同一知识库时向量检索仍无镜像（KB 不能当租户用）",
        not _leak(b1, mirror),
        f"命中 {len(b1)} 条，镜像 {len(_leak(b1, mirror))} 条",
    )


def _run_section_checks(res: Results, mirror: Mirror) -> None:
    print("\n=== 二、章节扩展（document_section 无 tenant 列，靠可信 document_id 集合）===")
    sr = SectionRetriever()

    # 1. 用镜像章节 id + 租户 1 文档白名单 → 必须为空
    out = sr.expand([mirror.probe_section_id], {mirror.src_doc_id})
    res.check(
        "expand(镜像 section_id, allowed={租户1 文档}) 为空",
        out == [],
        f"返回 {len(out)} 条",
    )

    # 2. 用租户 1 章节 id + 镜像文档白名单 → 必须为空
    out = sr.expand([mirror.src_section_id], {mirror.doc_id})
    res.check(
        "expand(租户1 section_id, allowed={镜像文档}) 为空",
        out == [],
        f"返回 {len(out)} 条",
    )

    # 3. 反证：用镜像章节 id + 镜像文档白名单 → 必须非空（证明探针章节真实存在）
    out = sr.expand([mirror.probe_section_id], {mirror.doc_id})
    res.check(
        "expand(镜像 section_id, allowed={镜像文档}) 非空（反证：探针章节真实存在）",
        len(out) >= 1 and all(x["document_id"] == mirror.doc_id for x in out),
        f"返回 {len(out)} 条，document_ids={sorted({x['document_id'] for x in out})}",
    )

    # 4. 租户 1 章节 + 租户 1 文档白名单 → 结果里绝不能混入镜像章节
    out = sr.expand([mirror.src_section_id], {mirror.src_doc_id})
    res.check(
        "expand(租户1 section_id, allowed={租户1 文档}) 结果不含镜像章节",
        all(x["document_id"] == mirror.src_doc_id for x in out),
        f"返回 {len(out)} 条，document_ids={sorted({x['document_id'] for x in out})}",
    )

    # 5. 空白名单：直接返回空，且**不访问 DB**（否则空集合会退化成全库扫描）
    import app.retrieval.section as section_mod

    original = section_mod.session_scope

    def _boom(*_a, **_kw):
        raise AssertionError("空 allowed 集合下不应访问数据库")

    section_mod.session_scope = _boom  # type: ignore[assignment]
    try:
        empty = sr.expand([mirror.probe_section_id, mirror.src_section_id], set())
    finally:
        section_mod.session_scope = original  # type: ignore[assignment]
    res.check(
        "expand(section_ids, allowed=空集) 返回空且不访问 DB",
        empty == [],
        f"返回 {len(empty)} 条",
    )


def _pattern_dict(row: SolutionPattern) -> dict[str, Any]:
    """构造与 `PatternRetriever.search` 输出同形状的 dict。

    用于**确定性**地触发 fingerprint 去重路径，不依赖向量召回是否恰好捞到该 pattern。
    """
    return {
        "pattern_id": row.id,
        "code": row.code,
        "name": row.name,
        "type": row.type,
        "industry": row.industry,
        "scenario": row.scenario,
        "summary": row.summary,
        "structure": row.structure or [],
        "content": row.content,
        "generalized_content": row.generalized_content,
        "fingerprint": row.fingerprint,
        "knowledge_base_id": row.knowledge_base_id,
        "score": 1.0,
        "score_type": "cosine",
    }


def _run_dedupe_checks(res: Results, mirror: Mirror) -> None:
    print("\n=== 三、fingerprint 去重 + pattern 知识节点（原泄漏点）===")
    svc = KnowledgeService()

    # --- A. search_patterns：source 溯源回填（召回驱动） ---
    pats = svc.search_patterns(mirror.query, tenant_id=TENANT_SRC, top_k=20, rerank=False)
    leak_src = [
        s
        for p in pats
        for s in p.get("sources", [])
        if s.get("document_id") == mirror.doc_id
    ]
    res.check(
        "search_patterns(tenant=1) 的 sources 无镜像 document_id",
        not leak_src,
        f"patterns={len(pats)}，越界 source={len(leak_src)}",
    )
    res.check(
        "search_patterns(tenant=1) 无镜像 pattern",
        all(p.get("pattern_id") != mirror.pattern_id for p in pats),
        f"pattern_ids={_ids(pats, 'pattern_id')[:8]}",
    )

    # --- B. fingerprint 去重：确定性构造「同 fingerprint 跨租户」场景 ---
    with session_scope() as session:
        src_row = session.get(SolutionPattern, mirror.src_pattern_id)
        src_dict = _pattern_dict(src_row)

        def _count_dup(tenant: int) -> int:
            return (
                session.execute(
                    select(func.count())
                    .select_from(SolutionPattern)
                    .where(
                        SolutionPattern.fingerprint == mirror.pattern_fp,
                        SolutionPattern.tenant_id == tenant,
                    )
                ).scalar()
                or 0
            )

        expected_src_dup = _count_dup(TENANT_SRC)
        expected_mirror_dup = _count_dup(TENANT_MIRROR)
        mirror_src_rows = (
            session.execute(
                select(func.count())
                .select_from(PatternSource)
                .where(PatternSource.pattern_id == mirror.pattern_id)
            ).scalar()
            or 0
        )

    # 以租户 1 身份去重：只应聚合同租户同指纹
    t1 = svc._enrich_pattern_sources([dict(src_dict)])
    d1 = svc._dedupe_patterns(t1, tenant_id=TENANT_SRC)
    rep1 = d1[0]
    res.check(
        "去重(tenant=1) 的 duplicate_count 只含同租户同指纹",
        rep1.get("duplicate_count") == expected_src_dup,
        f"duplicate_count={rep1.get('duplicate_count')} 期望={expected_src_dup}"
        f"（镜像 pattern 有 {mirror_src_rows} 条 source；若跨租户聚合会多算 1）",
    )
    res.check(
        "去重(tenant=1) 聚合出的 sources 无镜像 document_id",
        all(s.get("document_id") != mirror.doc_id for s in rep1.get("sources", [])),
        f"sources={len(rep1.get('sources', []))}"
        f" document_ids={sorted({s.get('document_id') for s in rep1.get('sources', [])})}",
    )

    # 反向对照：以租户 2 身份去重，只能聚合到镜像自己 —— 证明镜像 source 真实存在，
    # 上一条断言不是「因为镜像数据压根没写进去」而空转通过。
    d2 = svc._dedupe_patterns([dict(src_dict)], tenant_id=TENANT_MIRROR)
    rep2 = d2[0]
    src2_docs = {s.get("document_id") for s in rep2.get("sources", [])}
    res.check(
        "去重(tenant=2) 只聚合到镜像（反证：镜像 pattern_source 真实存在）",
        rep2.get("duplicate_count") == expected_mirror_dup
        and src2_docs == {mirror.doc_id}
        and mirror_src_rows > 0,
        f"duplicate_count={rep2.get('duplicate_count')} 期望={expected_mirror_dup}"
        f" sources 的 document_ids={sorted(src2_docs)}",
    )

    # --- C. _expand_pattern_sources：source 扩到兄弟章节也不得跨租户 ---
    expanded = svc._expand_pattern_sources([dict(p) for p in t1])
    bad = [
        s
        for p in expanded
        for s in p.get("sources", [])
        if s.get("document_id") == mirror.doc_id
    ]
    res.check(
        "_expand_pattern_sources(tenant=1) 无镜像章节",
        not bad,
        f"越界 source={len(bad)}",
    )


def _run_rag_checks(res: Results, mirror: Mirror) -> None:
    print("\n=== 四、rag 编排全链路（LLM 打桩，断言注入 prompt 的 context 无镜像）===")
    import app.llm.gateway as gateway_mod

    captured: dict[str, str] = {}

    class _StubLLM:
        def complete(self, prompt: str, system: str | None = None) -> str:
            captured["prompt"] = prompt
            return "[STUB]"

    original = gateway_mod.get_llm
    gateway_mod.get_llm = lambda: _StubLLM()  # type: ignore[assignment]
    try:
        result = KnowledgeService().rag(
            mirror.query,
            tenant_id=TENANT_SRC,
            pattern_top_k=20,
            chunk_top_k=20,
            rerank=False,  # reranker 只对已过滤候选重排，不引入新行；关掉以免加载大模型
        )
    finally:
        gateway_mod.get_llm = original  # type: ignore[assignment]

    ctx = result["context"]
    trace = result["trace"]
    res.check(
        "rag(tenant=1) evidences 无镜像 document_id",
        all(e.get("document_id") != mirror.doc_id for e in result["evidences"]),
        f"evidences={len(result['evidences'])}，trace.document_ids={trace['document_ids'][:8]}",
    )
    res.check(
        "rag(tenant=1) patterns 无镜像 pattern_id",
        mirror.pattern_id not in trace["pattern_ids"],
        f"pattern_ids={trace['pattern_ids'][:8]}",
    )
    res.check(
        "rag(tenant=1) sections 无镜像章节",
        all(s.get("document_id") != mirror.doc_id for s in result["sections"]),
        f"sections={len(result['sections'])}",
    )
    res.check(
        "rag(tenant=1) 注入 LLM 的 context 不含镜像哨兵",
        MIRROR_DOC_NAME not in ctx and f"文档#{mirror.doc_id}" not in ctx,
        f"context 长度={len(ctx)}",
    )


def _run_guard_checks(res: Results, mirror: Mirror) -> None:
    print("\n=== 五、入口守卫（漏传租户必须硬失败，不得退化为全库）===")
    from app.core.tenant import TenantScopeError, require_tenant_id

    def _expect_raise(fn, name: str) -> None:
        try:
            fn()
        except TenantScopeError as exc:
            res.check(name, True, f"按预期拒绝：{exc}")
        except Exception as exc:  # noqa: BLE001
            res.check(name, False, f"抛出非 TenantScopeError：{type(exc).__name__}: {exc}")
        else:
            res.check(name, False, "未抛错（危险：会退化成无租户边界检索）")

    _expect_raise(lambda: require_tenant_id(None, where="check"), "require_tenant_id(None) 被拒绝")
    _expect_raise(lambda: require_tenant_id(0, where="check"), "require_tenant_id(0) 被拒绝")
    _expect_raise(
        lambda: VectorRetriever().search([0.0] * 8, tenant_id=None, top_k=1),  # type: ignore[arg-type]
        "VectorRetriever.search(tenant_id=None) 被拒绝",
    )
    _expect_raise(
        lambda: KeywordRetriever().search("x", tenant_id=None, top_k=1),  # type: ignore[arg-type]
        "KeywordRetriever.search(tenant_id=None) 被拒绝",
    )
    _expect_raise(
        lambda: PatternRetriever().search([0.0] * 8, tenant_id=None, top_k=1),  # type: ignore[arg-type]
        "PatternRetriever.search(tenant_id=None) 被拒绝",
    )
    _expect_raise(
        lambda: KnowledgeService().search(mirror.query, tenant_id=None),  # type: ignore[arg-type]
        "KnowledgeService.search(tenant_id=None) 被拒绝",
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _load_invariants() -> list[tuple[str, str]]:
    """按文件路径加载普查脚本的一致性不变量清单。

    `scripts/` 不是 Python 包（无 `__init__.py`），直接 `import scripts.xxx` 会失败，
    故用 importlib 从文件加载；只取常量，不执行其 `main()`。
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent / "census_tenant_migration.py"
    spec = importlib.util.spec_from_file_location("_census_tenant_migration", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"[ABORT] 无法加载一致性不变量来源：{path}")
    module = importlib.util.module_from_spec(spec)
    # dataclass 装饰器需要能通过 cls.__module__ 回查 sys.modules，先注册再执行。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module.INVARIANTS


def main() -> int:
    parser = argparse.ArgumentParser(description="双租户同文本/同 fingerprint 零串库验收")
    parser.add_argument(
        "--keep-mirror",
        action="store_true",
        help="保留镜像数据（排查用）；默认清理",
    )
    args = parser.parse_args()

    setup_logging()
    res = Results()

    print("=" * 78)
    print("批次 A §4.6 验收：双租户同文本 / 同 fingerprint 零串库（真实库）")
    print("=" * 78)

    with session_scope() as session:
        _purge_mirror(session)  # 清上一轮残留
        before = _snapshot_counts(session)
    print(f"\n=== 零、基线行数 ===\n  {before}")

    with session_scope() as session:
        mirror = _build_mirror(session)
    print("\n=== 镜像数据已构造（仅 tenant_id 与租户 1 不同，其余全同）===")
    print(f"  镜像 document_id = {mirror.doc_id}  知识库 = {mirror.kb_id}")
    print(f"  镜像 section 数  = {len(mirror.section_ids)}")
    print(f"  镜像 chunk 数    = {len(mirror.chunk_ids)}（同文本 + 同 embedding）")
    print(f"  镜像 pattern_id  = {mirror.pattern_id}  fingerprint = {mirror.pattern_fp}")
    print(f"  探针章节         = 租户1#{mirror.src_section_id} ↔ 镜像#{mirror.probe_section_id}")

    try:
        _run_retrieval_checks(res, mirror)
        _run_section_checks(res, mirror)
        _run_dedupe_checks(res, mirror)
        _run_rag_checks(res, mirror)
        _run_guard_checks(res, mirror)
    finally:
        if args.keep_mirror:
            print("\n[INFO] --keep-mirror：保留镜像数据，未清理。")
        else:
            with session_scope() as session:
                _purge_mirror(session)
            print("\n=== 六、清理与复核 ===")
            with session_scope() as session:
                after = _snapshot_counts(session)
                print(f"  清理后行数：{after}")
                res.check(
                    "清理后各表行数与基线一致",
                    after == before,
                    f"before={before}\n         after ={after}",
                )
                left = session.execute(
                    text("select count(*) from document where id = :d"), {"d": MIRROR_DOC_ID}
                ).scalar()
                res.check("镜像 document 已清除", (left or 0) == 0, f"残留 {left}")

                # 复用普查脚本的一致性不变量
                invariants = _load_invariants()

                broken = []
                for name, sql in invariants:
                    cnt = session.execute(text(sql)).scalar() or 0
                    if cnt:
                        broken.append((name, cnt))
                res.check(
                    "清理后一致性不变量全部为 0",
                    not broken,
                    f"违规项={broken}" if broken else f"{len(invariants)} 项全部为 0",
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
    print(f"[PASS] {total}/{total} 全部通过：双租户同文本、同 fingerprint 零串库。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
