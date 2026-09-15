"""租户迁移普查与可信回填（批次 A / P0）。

背景：`tenant_id` 在本期从「可选过滤字段」升级为「检索硬边界」。升级前写入的历史数据可能
存在 tenant NULL、孤儿、跨租户/跨库冲突。本脚本先**统计**，再按「只回填可信关联」的规则修补。

用法::

    uv run python scripts/census_tenant_migration.py              # 只读普查（默认）
    uv run python scripts/census_tenant_migration.py --apply-backfill   # 执行可信回填

规则（与计划 §4.1 一致）::

    - 只能回填「可信关联」：chunk/pattern 的 tenant 可以从其**父 document** 推导出来。
    - 禁止补默认租户：document.tenant_id 为 NULL 时**不猜**，只报告，由人工隔离或重建。
    - 孤儿行（父记录不存在）只报告，不删除、不改写。
    - 跨租户/跨库冲突只报告，不自动选边。
    - `document_section` / `pattern_source` 没有 tenant 列（设计如此），本脚本不碰。

脚本可重复执行：回填只针对 NULL，已填过的行不会被再次改写。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import text

from app.core.database import session_scope
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# 普查项：(名称, SQL)
# --------------------------------------------------------------------------- #
CENSUS: list[tuple[str, str]] = [
    ("document 总数", "select count(*) from document"),
    ("document.tenant_id IS NULL", "select count(*) from document where tenant_id is null"),
    (
        "document 租户分布",
        "select coalesce(tenant_id::text, '<NULL>') as tenant, count(*) from document"
        " group by 1 order by 1",
    ),
    ("document_chunk 总数", "select count(*) from document_chunk"),
    ("document_chunk.tenant_id IS NULL", "select count(*) from document_chunk where tenant_id is null"),
    (
        "document_chunk 租户分布",
        "select coalesce(tenant_id::text, '<NULL>') as tenant, count(*) from document_chunk"
        " group by 1 order by 1",
    ),
    ("solution_pattern 总数", "select count(*) from solution_pattern"),
    ("solution_pattern.tenant_id IS NULL", "select count(*) from solution_pattern where tenant_id is null"),
    (
        "solution_pattern 租户分布",
        "select coalesce(tenant_id::text, '<NULL>') as tenant, count(*) from solution_pattern"
        " group by 1 order by 1",
    ),
    ("document_section 总数(无 tenant 列)", "select count(*) from document_section"),
    ("pattern_source 总数(无 tenant 列)", "select count(*) from pattern_source"),
]


# --------------------------------------------------------------------------- #
# 一致性不变量：(名称, SQL)。全部应为 0。
# --------------------------------------------------------------------------- #
INVARIANTS: list[tuple[str, str]] = [
    (
        "chunk 的父 document 缺失(孤儿)",
        "select count(*) from document_chunk c left join document d on d.id = c.document_id"
        " where d.id is null",
    ),
    (
        "chunk 与父 document 租户不一致",
        "select count(*) from document_chunk c join document d on d.id = c.document_id"
        " where c.tenant_id is distinct from d.tenant_id",
    ),
    (
        "chunk 与父 document 知识库不一致",
        "select count(*) from document_chunk c join document d on d.id = c.document_id"
        " where c.knowledge_base_id is distinct from d.knowledge_base_id",
    ),
    (
        "同一 document_id 的 chunk 落在多个租户",
        "select count(*) from (select document_id from document_chunk group by document_id"
        " having count(distinct tenant_id) > 1) t",
    ),
    (
        "section 的父 document 缺失(孤儿)",
        "select count(*) from document_section s left join document d on d.id = s.document_id"
        " where d.id is null",
    ),
    (
        "section 的父 document 租户不唯一",
        "select count(*) from (select s.document_id from document_section s"
        " join document d on d.id = s.document_id group by s.document_id"
        " having count(distinct d.tenant_id) <> 1) t",
    ),
    (
        "chunk.section_id 指向不存在的章节",
        "select count(*) from document_chunk c where c.section_id is not null"
        " and not exists (select 1 from document_section s where s.id = c.section_id)",
    ),
    (
        "chunk 与其 section 的 document 不一致",
        "select count(*) from document_chunk c join document_section s on s.id = c.section_id"
        " where s.document_id is distinct from c.document_id",
    ),
    (
        "pattern_source 的父 pattern 缺失(孤儿)",
        "select count(*) from pattern_source ps left join solution_pattern p on p.id = ps.pattern_id"
        " where p.id is null",
    ),
    (
        "pattern_source 的 document 缺失(孤儿)",
        "select count(*) from pattern_source ps where ps.document_id is not null"
        " and not exists (select 1 from document d where d.id = ps.document_id)",
    ),
    (
        "pattern 与其 source.document 租户不一致",
        "select count(*) from pattern_source ps join solution_pattern p on p.id = ps.pattern_id"
        " join document d on d.id = ps.document_id"
        " where p.tenant_id is distinct from d.tenant_id",
    ),
    (
        "pattern 与其 source.document 知识库不一致",
        "select count(*) from pattern_source ps join solution_pattern p on p.id = ps.pattern_id"
        " join document d on d.id = ps.document_id"
        " where p.knowledge_base_id is distinct from d.knowledge_base_id",
    ),
]


@dataclass(frozen=True)
class Backfill:
    """一条可信回填规则：只补 NULL，且来源必须是已确认归属的父记录。"""

    name: str
    preview_sql: str
    apply_sql: str


# 只回填「能从父 document 推导出唯一归属」的行；不涉及默认租户。
BACKFILLS: list[Backfill] = [
    Backfill(
        name="document_chunk.tenant_id ← 父 document.tenant_id",
        preview_sql=(
            "select count(*) from document_chunk c join document d on d.id = c.document_id"
            " where c.tenant_id is null and d.tenant_id is not null"
        ),
        apply_sql=(
            "update document_chunk c set tenant_id = d.tenant_id"
            " from document d where d.id = c.document_id"
            " and c.tenant_id is null and d.tenant_id is not null"
        ),
    ),
    Backfill(
        name="document_chunk.knowledge_base_id ← 父 document.knowledge_base_id",
        preview_sql=(
            "select count(*) from document_chunk c join document d on d.id = c.document_id"
            " where c.knowledge_base_id is null and d.knowledge_base_id is not null"
        ),
        apply_sql=(
            "update document_chunk c set knowledge_base_id = d.knowledge_base_id"
            " from document d where d.id = c.document_id"
            " and c.knowledge_base_id is null and d.knowledge_base_id is not null"
        ),
    ),
    Backfill(
        name="solution_pattern.tenant_id ← 其 source 文档的唯一租户",
        preview_sql=(
            "select count(*) from solution_pattern p where p.tenant_id is null and exists ("
            "  select 1 from pattern_source ps join document d on d.id = ps.document_id"
            "  where ps.pattern_id = p.id and d.tenant_id is not null"
            "  group by ps.pattern_id having count(distinct d.tenant_id) = 1)"
        ),
        apply_sql=(
            "update solution_pattern p set tenant_id = src.tenant_id from ("
            "  select ps.pattern_id, min(d.tenant_id) as tenant_id"
            "  from pattern_source ps join document d on d.id = ps.document_id"
            "  where d.tenant_id is not null"
            "  group by ps.pattern_id having count(distinct d.tenant_id) = 1) src"
            " where src.pattern_id = p.id and p.tenant_id is null"
        ),
    ),
    Backfill(
        name="solution_pattern.knowledge_base_id ← 其 source 文档的唯一知识库",
        preview_sql=(
            "select count(*) from solution_pattern p where p.knowledge_base_id is null and exists ("
            "  select 1 from pattern_source ps join document d on d.id = ps.document_id"
            "  where ps.pattern_id = p.id and d.knowledge_base_id is not null"
            "  group by ps.pattern_id having count(distinct d.knowledge_base_id) = 1)"
        ),
        apply_sql=(
            "update solution_pattern p set knowledge_base_id = src.knowledge_base_id from ("
            "  select ps.pattern_id, min(d.knowledge_base_id) as knowledge_base_id"
            "  from pattern_source ps join document d on d.id = ps.document_id"
            "  where d.knowledge_base_id is not null"
            "  group by ps.pattern_id having count(distinct d.knowledge_base_id) = 1) src"
            " where src.pattern_id = p.id and p.knowledge_base_id is null"
        ),
    ),
]


def _print_census(session) -> dict[str, int]:
    print("\n=== 一、数据普查 ===")
    totals: dict[str, int] = {}
    for name, sql in CENSUS:
        rows = session.execute(text(sql)).all()
        if len(rows) == 1 and len(rows[0]) == 1:
            totals[name] = rows[0][0]
            print(f"  {name:44} {rows[0][0]}")
        else:
            print(f"  {name:44} {rows}")
    return totals


def _print_invariants(session) -> dict[str, int]:
    print("\n=== 二、一致性不变量（全部应为 0）===")
    violations: dict[str, int] = {}
    for name, sql in INVARIANTS:
        count = session.execute(text(sql)).scalar() or 0
        violations[name] = count
        flag = "OK" if count == 0 else "!!"
        print(f"  [{flag}] {name:44} {count}")
    return violations


def _print_backfill_plan(session, apply: bool) -> None:
    print(f"\n=== 三、可信回填（{'执行' if apply else '只读预估，未执行'}）===")
    for rule in BACKFILLS:
        pending = session.execute(text(rule.preview_sql)).scalar() or 0
        if not apply:
            print(f"  [待回填 {pending:>6}] {rule.name}")
            continue
        result = session.execute(text(rule.apply_sql))
        print(f"  [已回填 {result.rowcount:>6}] {rule.name}（预估 {pending}）")


def _print_manual_items(session) -> None:
    """必须人工处理的项：不猜归属、不删数据。"""
    print("\n=== 四、需人工处理（脚本不自动修复）===")
    manual = [
        (
            "document.tenant_id IS NULL（归属未知，隔离或重建，禁止补默认租户）",
            "select count(*) from document where tenant_id is null",
        ),
        (
            "孤儿 chunk（父 document 不存在）",
            "select count(*) from document_chunk c left join document d on d.id = c.document_id"
            " where d.id is null",
        ),
        (
            "孤儿 pattern_source（父 pattern 不存在）",
            "select count(*) from pattern_source ps left join solution_pattern p on p.id = ps.pattern_id"
            " where p.id is null",
        ),
        (
            "孤儿 section（父 document 不存在）",
            "select count(*) from document_section s left join document d on d.id = s.document_id"
            " where d.id is null",
        ),
        (
            "source 文档租户不唯一的 pattern（无法可信推导归属）",
            "select count(*) from solution_pattern p where p.tenant_id is null and exists ("
            "  select 1 from pattern_source ps join document d on d.id = ps.document_id"
            "  where ps.pattern_id = p.id and d.tenant_id is not null"
            "  group by ps.pattern_id having count(distinct d.tenant_id) > 1)",
        ),
    ]
    for name, sql in manual:
        print(f"  {name:48} {session.execute(text(sql)).scalar() or 0}")


def main() -> None:
    parser = argparse.ArgumentParser(description="租户迁移普查与可信回填")
    parser.add_argument(
        "--apply-backfill",
        action="store_true",
        help="执行可信回填（不加此参数时全程只读）",
    )
    args = parser.parse_args()

    setup_logging()
    print("=" * 78)
    print(f"租户迁移普查 mode={'APPLY' if args.apply_backfill else 'READ-ONLY'}")
    print("=" * 78)

    with session_scope() as session:
        _print_census(session)
        violations = _print_invariants(session)
        _print_backfill_plan(session, apply=args.apply_backfill)
        if args.apply_backfill:
            # 回填后重查不变量，确保没有把数据改坏
            print("\n=== 五、回填后不变量复核 ===")
            for name, sql in INVARIANTS:
                count = session.execute(text(sql)).scalar() or 0
                flag = "OK" if count == 0 else "!!"
                print(f"  [{flag}] {name:44} {count}")
        _print_manual_items(session)

    broken = [k for k, v in violations.items() if v]
    print("\n" + "=" * 78)
    if broken:
        print(f"[WARN] 存在 {len(broken)} 项一致性违规，需人工确认后再上线：")
        for name in broken:
            print(f"       - {name}")
    else:
        print("[PASS] 一致性不变量全部为 0")
    if not args.apply_backfill:
        print("[INFO] 本次为只读模式，未修改任何数据。")
    print("=" * 78)


if __name__ == "__main__":
    main()
