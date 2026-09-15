"""批次 A §4.4 验收：默认问答范围与 tender 隔离（真实库端到端）。

验收原文（开发计划 §4.4）::

    本期安全基线即统一 tender_doc → tender，并清理/隔离存量 tender pattern：
    - 默认 QA 类型：historical_bid / enterprise_profile / qualification / project_case /
      product_manual / technical_document / regulation / other；
    - 默认排除 tender；
    - Java 解析 tender 时 withPatterns=false，Python 持久化/调度层再次强制拒绝 tender pattern；
    - pattern 召回必须通过来源 document 约束默认类型，不能只过滤 solution_pattern.tenant_id。

为什么单测不够
--------------
`tests/test_doc_scope.py` 断言的是「SQL 里带了来源类型条件」，证明的是**构造正确**；
本脚本证明的是「真实库 + 真实 pgvector 下，一个 active 状态的 tender 来源 pattern
确实召不回来」，并给出**反证**（同向量、同文本的非 tender pattern 必须能召回），
避免测试空转。

关键设计
--------
1. 哨兵 pattern 用**真实 chunk 的 embedding**，与对照 pattern 共用同一向量 ——
   干扰强度拉满：任何一处漏加来源类型条件，tender 哨兵都会以距离 0 排到第一。
2. tender 哨兵的 `status` 保持 `active`。**这是刻意的**：
   要证明隔离靠的是「来源文档类型」而不是「把 status 改成 quarantined」——
   如果只靠 status，任何一次 status 回填都会让 tender 内容重新进问答。
3. 另外验一个混合来源 pattern（tender + historical_bid）**必须仍能召回** ——
   证明收紧没有误伤合法数据。

用法::

    uv run python scripts/verify_qa_doc_scope.py
    uv run python scripts/verify_qa_doc_scope.py --keep     # 排查用，保留哨兵数据

退出码：0 = 全部通过；1 = 存在失败项。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from app.core.database import session_scope
from app.core.doc_types import QA_DOCUMENT_TYPES, TENDER_DOCUMENT_TYPE
from app.core.logging import get_logger, setup_logging
from app.knowledge.service import KnowledgeService
from app.models.pattern import PatternSource, SolutionPattern
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.pattern import PatternRetriever

logger = get_logger(__name__)

# --- 固定哨兵：让脚本可重复执行（每次先清残留，再重建） ---
TENANT = 3
DOC_TENDER = 9_200_001
DOC_BID = 9_200_002
DOC_MIXED = 9_200_003

CODE_TENDER_ONLY = "SCOPE-TENDER-ONLY"
CODE_BID_ONLY = "SCOPE-BID-ONLY"
CODE_MIXED = "SCOPE-MIXED"
FP_SHARED = "scope-shared-fingerprint"  # 故意让 tender 与合法 pattern 共用指纹

QUARANTINED_STATUS = "quarantined"


# --------------------------------------------------------------------------- #
# 断言结果收集
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

    def note(self, name: str, detail: str) -> None:
        """记录「已知边界」，不计入成败。"""
        self.checks.append((name, True, detail))
        print(f"  [NOTE] {name}")
        print(f"         {detail}")

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]


# --------------------------------------------------------------------------- #
# 哨兵构造 / 清理
# --------------------------------------------------------------------------- #
def _purge(session) -> None:
    """按哨兵清除数据（幂等，开跑前清残留与收尾共用）。"""
    doc_ids = [DOC_TENDER, DOC_BID, DOC_MIXED]
    codes = [CODE_TENDER_ONLY, CODE_BID_ONLY, CODE_MIXED]
    session.execute(
        text("delete from pattern_source where document_id = any(:docs)"), {"docs": doc_ids}
    )
    session.execute(
        text(
            "delete from pattern_source where pattern_id in"
            " (select id from solution_pattern where code = any(:codes))"
        ),
        {"codes": codes},
    )
    session.execute(
        text("delete from solution_pattern where code = any(:codes)"), {"codes": codes}
    )
    session.execute(
        text("delete from document_pattern_job where document_id = any(:docs)"), {"docs": doc_ids}
    )
    session.execute(
        text("delete from document_index_guard where document_id = any(:docs)"), {"docs": doc_ids}
    )
    session.execute(text("delete from document where id = any(:docs)"), {"docs": doc_ids})


def _snapshot_counts(session) -> dict[str, int]:
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


def _borrow_embedding(session) -> list[float]:
    """借一条真实 chunk 的 embedding 当向量（保证维度与真实数据一致）。"""
    row = session.execute(
        text(
            "select embedding::text from document_chunk"
            " where embedding is not null order by id asc limit 1"
        )
    ).scalar()
    if not row:
        raise SystemExit("[ABORT] 库里没有带 embedding 的 chunk，无法构造哨兵向量")
    return [float(x) for x in row.strip("[]").split(",")]


@dataclass
class Sentinels:
    vector: list[float]
    query: str
    tender_pattern_id: int
    bid_pattern_id: int
    mixed_pattern_id: int


def _build(session) -> Sentinels:
    """构造三个哨兵 pattern + 对应来源文档。"""
    vector = _borrow_embedding(session)

    # 来源文档：只有 id / document_type / tenant_id 参与本次断言
    for doc_id, doc_type, name in (
        (DOC_TENDER, TENDER_DOCUMENT_TYPE, "[SCOPE] 招标文件哨兵"),
        (DOC_BID, "historical_bid", "[SCOPE] 历史标书哨兵"),
        (DOC_MIXED, "historical_bid", "[SCOPE] 混合来源哨兵"),
    ):
        session.execute(
            text(
                """
                insert into document (id, tenant_id, name, file_name, file_type, document_type, status)
                values (:id, :t, :name, :name, 'docx', :dt, 'embedded')
                on conflict (id) do update set document_type = excluded.document_type
                """
            ),
            {"id": doc_id, "t": TENANT, "name": name, "dt": doc_type},
        )

    # 让 tender 哨兵与合法哨兵**共用同一指纹**：
    # 这样 `_dedupe_patterns` 的 fingerprint 回查如果漏了来源类型条件，
    # 就会把 tender 哨兵的 pattern_source（document_id=DOC_TENDER）聚合进合法结果里。
    def add_pattern(code: str, name: str, fingerprint: str) -> int:
        session.execute(
            text(
                """
                insert into solution_pattern
                    (tenant_id, name, code, type, summary, fingerprint, version, status, embedding)
                values (:t, cast(:name as varchar), :code, 'methodology', cast(:summary as text),
                        :fp, 1, 'active', cast(:vec as vector))
                returning id
                """
            ),
            {
                "t": TENANT,
                "name": name,
                "summary": name,
                "code": code,
                "fp": fingerprint,
                "vec": str(vector),
            },
        )
        return int(
            session.execute(
                text("select id from solution_pattern where code = :code"), {"code": code}
            ).scalar()
        )

    tender_pid = add_pattern(CODE_TENDER_ONLY, "[SCOPE] 招标文件方案", FP_SHARED)
    bid_pid = add_pattern(CODE_BID_ONLY, "[SCOPE] 历史标书方案", FP_SHARED)
    mixed_pid = add_pattern(CODE_MIXED, "[SCOPE] 混合来源方案", "scope-mixed-fingerprint")

    def add_source(pattern_id: int, document_id: int) -> None:
        session.execute(
            text(
                "insert into pattern_source (pattern_id, document_id, source_type, sort_order)"
                " values (:p, :d, 'section', 0)"
            ),
            {"p": pattern_id, "d": document_id},
        )

    add_source(tender_pid, DOC_TENDER)
    add_source(bid_pid, DOC_BID)
    # 混合：同时被 tender 与非 tender 文档引用
    add_source(mixed_pid, DOC_TENDER)
    add_source(mixed_pid, DOC_MIXED)

    return Sentinels(
        vector=vector,
        query="[SCOPE] 方案组件来源类型验收",
        tender_pattern_id=tender_pid,
        bid_pattern_id=bid_pid,
        mixed_pattern_id=mixed_pid,
    )


# --------------------------------------------------------------------------- #
# 断言辅助
# --------------------------------------------------------------------------- #
def _load_quarantine_module() -> Any:
    """按文件路径加载同目录脚本。

    `scripts/` 没有 `__init__.py`，不是包，`from scripts import x` 会 `ModuleNotFoundError`；
    而且 `@dataclass` 装饰器需要能通过 `cls.__module__` 回查 `sys.modules`，
    所以 `exec_module` 之前必须先塞进 `sys.modules`，结束再弹出。
    """
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).with_name("quarantine_tender_patterns.py")
    spec = importlib.util.spec_from_file_location("_verify_quarantine_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _codes(hits: list[dict[str, Any]]) -> list[str]:
    return [h.get("code") for h in hits]


def _pattern_ids(hits: list[dict[str, Any]]) -> set[int]:
    return {h.get("pattern_id") for h in hits if h.get("pattern_id") is not None}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="§4.4 默认问答范围与 tender 隔离验收")
    parser.add_argument("--keep", action="store_true", help="保留哨兵数据（排查用）")
    args = parser.parse_args()

    results = Results()

    with session_scope() as session:
        _purge(session)
    with session_scope() as session:
        baseline = _snapshot_counts(session)
        sent = _build(session)

    print("\n=== 0. 哨兵就绪 ===")
    print(
        f"  租户={TENANT}  tender-only pattern id={sent.tender_pattern_id}  "
        f"bid-only id={sent.bid_pattern_id}  mixed id={sent.mixed_pattern_id}"
    )
    print(f"  QA 白名单：{list(QA_DOCUMENT_TYPES)}")

    retriever = PatternRetriever()

    # --- 1. 反证：同向量的合法 pattern 必须能召回 ---
    print("\n=== 1. 反证（证明测试不是空转） ===")
    hits = retriever.search(sent.vector, tenant_id=TENANT, top_k=10, status=None)
    codes = _codes(hits)
    results.check(
        "同向量、非 tender 来源的哨兵 pattern 能被召回",
        sent.bid_pattern_id in _pattern_ids(hits),
        f"召回 code={codes}",
    )

    # --- 2. 代码级过滤：tender-only pattern 召不回（且 status 仍是 active） ---
    print("\n=== 2. 代码级来源类型过滤（pattern 的 status 保持 active） ===")
    with session_scope() as session:
        status = session.execute(
            text("select status from solution_pattern where id = :p"),
            {"p": sent.tender_pattern_id},
        ).scalar()
    results.check(
        "哨兵 tender-only pattern 处于 active 状态（隔离不靠 status）",
        status == "active",
        f"status={status}",
    )
    results.check(
        "PatternRetriever.search 不返回 tender-only pattern",
        sent.tender_pattern_id not in _pattern_ids(hits),
        f"召回 code={codes}",
    )

    hybrid = HybridRetriever()
    h_hits = hybrid.search_patterns(
        sent.query, tenant_id=TENANT, top_k=10, rerank=False, pattern_types=["methodology"]
    )
    results.check(
        "HybridRetriever.search_patterns 不返回 tender-only pattern",
        sent.tender_pattern_id not in _pattern_ids(h_hits),
        f"召回 code={_codes(h_hits)}",
    )

    forced = retriever.search(
        sent.vector,
        tenant_id=TENANT,
        top_k=10,
        status=None,
        document_types=[TENDER_DOCUMENT_TYPE],
    )
    results.check(
        "显式传 document_types=['tender'] 也不能放宽到 tender",
        sent.tender_pattern_id not in _pattern_ids(forced),
        f"召回 code={_codes(forced)}",
    )

    svc = KnowledgeService()
    svc_hits = svc.search_patterns(
        sent.query, tenant_id=TENANT, top_k=10, rerank=False, pattern_types=["methodology"]
    )
    results.check(
        "KnowledgeService.search_patterns 不返回 tender-only pattern",
        sent.tender_pattern_id not in _pattern_ids(svc_hits),
        f"召回 code={_codes(svc_hits)}",
    )

    # --- 3. fingerprint 回查不得把 tender 来源聚合进来 ---
    print("\n=== 3. fingerprint 去重不得跨来源类型聚合 ===")
    legit = [h for h in svc_hits if h.get("pattern_id") == sent.bid_pattern_id]
    if legit:
        deduped = KnowledgeService._dedupe_patterns(legit, tenant_id=TENANT)
        src_docs = {
            s.get("document_id") for d in deduped for s in (d.get("sources") or [])
        }
        results.check(
            "同指纹的 tender pattern 来源未被聚合进合法 pattern 的 sources",
            DOC_TENDER not in src_docs,
            f"sources 覆盖 document_id={sorted(x for x in src_docs if x is not None)}",
        )
    else:
        results.check("同指纹的 tender pattern 来源未被聚合进合法 pattern 的 sources", False,
                      "合法哨兵未召回，无法验证")

    # --- 4. 混合来源 pattern 不得被误伤 ---
    print("\n=== 4. 混合来源 pattern 不得被误伤 ===")
    results.check(
        "tender + 非 tender 双来源的 pattern 仍可召回",
        sent.mixed_pattern_id in _pattern_ids(hits),
        f"召回 code={codes}",
    )

    # --- 5. 调度层强制拒绝 ---
    print("\n=== 5. 持久化/调度层强制拒绝 tender pattern ===")
    import app.knowledge.ingest as ingest_module

    class _FakeResult:
        sections: list[Any] = []

    with session_scope() as session:
        jobs_before = session.execute(
            text("select count(*) from document_pattern_job where document_id = :d"),
            {"d": DOC_TENDER},
        ).scalar()
    ingest_module._spawn_pattern_extraction(
        _FakeResult(),
        {},
        document_id=DOC_TENDER,
        index_version=1,
        input_hash="scope-verify",
        tenant_id=TENANT,
        document_type=TENDER_DOCUMENT_TYPE,
    )
    with session_scope() as session:
        jobs_after = session.execute(
            text("select count(*) from document_pattern_job where document_id = :d"),
            {"d": DOC_TENDER},
        ).scalar()
    results.check(
        "_spawn_pattern_extraction 对 tender 直接返回，未占用 pattern job",
        jobs_before == 0 and jobs_after == 0,
        f"job 行数 {jobs_before} → {jobs_after}",
    )

    from app.pattern.indexer import SolutionPatternIndexer

    saved = SolutionPatternIndexer.__new__(SolutionPatternIndexer).save(
        [(object(), object())],
        id_map={},
        document_id=DOC_TENDER,
        tenant_id=TENANT,
        document_type=TENDER_DOCUMENT_TYPE,
    )
    results.check("SolutionPatternIndexer.save 对 tender 返回 0（不写入）", saved == 0)

    # --- 6. 隔离脚本可执行且幂等 ---
    print("\n=== 6. 隔离脚本（quarantine_tender_patterns） ===")
    quarantine = _load_quarantine_module()

    first = quarantine._apply()
    second = quarantine._apply()
    with session_scope() as session:
        q_status = session.execute(
            text("select status from solution_pattern where id = :p"),
            {"p": sent.tender_pattern_id},
        ).scalar()
        mixed_status = session.execute(
            text("select status from solution_pattern where id = :p"),
            {"p": sent.mixed_pattern_id},
        ).scalar()
    results.check(
        "tender-only pattern 被脚本隔离为 quarantined",
        q_status == QUARANTINED_STATUS,
        f"status={q_status}（首轮 {first} / 次轮 {second}）",
    )
    results.check(
        "混合来源 pattern 未被脚本误隔离",
        mixed_status == "active",
        f"status={mixed_status}",
    )
    results.check("隔离脚本可重复执行（第二轮改动为 0）",
                  second.get("quarantined_patterns", -1) == 0, f"second={second}")

    # 隔离后再验一次召回（双重保险）
    after_hits = retriever.search(sent.vector, tenant_id=TENANT, top_k=10, status=None)
    results.check(
        "隔离后 tender-only pattern 仍不可召回",
        sent.tender_pattern_id not in _pattern_ids(after_hits),
        f"召回 code={_codes(after_hits)}",
    )

    # --- 7. 已知边界：证据通道 ---
    print("\n=== 7. 边界说明 ===")
    results.note(
        "证据通道（chunk）本批次未加默认类型过滤",
        "tender 文档的 chunk 目前仍可被 VectorRetriever/KeywordRetriever 召回；"
        "「QA 默认排除 tender」对证据通道的收敛属于批次 B（prepare_qa 入口），"
        "本批次只收口 pattern 通道与持久化/调度层。",
    )

    # --- 8. 清理与复核 ---
    if args.keep:
        print("\n[KEEP] 保留哨兵数据，跳过清理")
    else:
        with session_scope() as session:
            _purge(session)
        with session_scope() as session:
            after = _snapshot_counts(session)
        print("\n=== 8. 清理复核 ===")
        for table, before_count in baseline.items():
            results.check(
                f"{table} 行数复原",
                after[table] == before_count,
                f"{before_count} → {after[table]}",
            )

    print("\n" + "=" * 72)
    total = len(results.checks)
    failed = results.failed
    if failed:
        print(f"[FAIL] {len(failed)}/{total} 项未通过：")
        for name, _, detail in failed:
            print(f"  - {name} {detail}")
        sys.exit(1)
    print(f"[PASS] {total}/{total} 项全部通过")


if __name__ == "__main__":
    main()
