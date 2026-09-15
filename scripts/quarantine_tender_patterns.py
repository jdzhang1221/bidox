"""招标文件（tender）来源数据的收敛与隔离（批次 A / §4.4）。

背景
----
`tender`（招标文件）是**本次项目的约束来源**，不是可复用的历史知识。把它混进企业知识问答
会带来两类问题：

1. 招标文件里的具体要求/评分办法会被当成「企业事实」复述给下一个项目；
2. 招标文件常含代理机构、采购人、预算等信息，属于不该跨项目复用的内容。

因此 §4.4 要求：统一 `tender_doc → tender`，并**清理/隔离存量 tender pattern**。

本脚本做三件事
--------------
1. **类型收敛**：`document.document_type` 的 `tender_doc` → `tender`。
   早期字典写的是 `tender_doc`，而代码统一用 `tender`；不一致会让按类型的过滤**静默失效**。
2. **隔离 tender-only pattern**：把所有来源都是 tender 的 pattern 置为
   `status='quarantined'`（**可逆**，不物理删除）。`PatternRetriever` 默认只召回
   `status='active'`，因此它们不会再进入任何问答上下文。
3. **不动混合来源 pattern**：既被 tender 又被非 tender 文档引用的 pattern 是**合法可复用**的
   （非 tender 来源仍在白名单内），新代码的「来源文档类型」过滤已让 tender 那一侧不参与召回，
   不需要也不应该动它。

为什么用 `status` 而不是删行
---------------------------
物理删除不可逆，而「存量 tender pattern」本身就是历史抽取产物、未来可能有别的用途。
`status='quarantined'` 既能确定性隔离，又能在需要时一条 SQL 复原。

为什么代码修好之后还要跑这个脚本
--------------------------------
代码侧（`pattern_source_type_clause`）已经保证 tender 来源不会被召回 —— 这个脚本是
**纵深防御 + 数据卫生**：让「库里有多少 tender 来源的 pattern」这件事在数据层面就可审计，
而不是每次都要靠读代码来确认。

用法::

    uv run python scripts/quarantine_tender_patterns.py            # 只读普查（默认）
    uv run python scripts/quarantine_tender_patterns.py --apply    # 执行收敛与隔离
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.core.database import session_scope
from app.core.doc_types import QA_DOCUMENT_TYPES, TENDER_DOCUMENT_TYPE
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)

# 历史遗留的招标文件类型值
LEGACY_TENDER_TYPE = "tender_doc"
# 隔离态（PatternRetriever 默认只召回 active）
QUARANTINED_STATUS = "quarantined"


def _scalar(session, sql: str, **params) -> int:
    value = session.execute(text(sql), params).scalar()
    return int(value or 0)


def _census() -> dict[str, int]:
    """只读普查：当前 tender 相关数据分布。"""
    with session_scope() as session:
        has_document = session.execute(
            text("SELECT to_regclass('public.document') IS NOT NULL")
        ).scalar()
        if not has_document:
            raise SystemExit("AI 库中不存在 document 表，请先完成建表/迁移")

        stats = {
            "document_tender_legacy": _scalar(
                session,
                "SELECT count(*) FROM document WHERE document_type = :t",
                t=LEGACY_TENDER_TYPE,
            ),
            "document_tender": _scalar(
                session,
                "SELECT count(*) FROM document WHERE document_type = :t",
                t=TENDER_DOCUMENT_TYPE,
            ),
            "tender_source_rows": _scalar(
                session,
                """
                SELECT count(*)
                FROM pattern_source ps
                JOIN document d ON d.id = ps.document_id
                WHERE d.document_type IN (:t, :legacy)
                """,
                t=TENDER_DOCUMENT_TYPE,
                legacy=LEGACY_TENDER_TYPE,
            ),
            # 全部来源都是 tender 的 pattern（可安全隔离）
            "tender_only_patterns": _scalar(
                session,
                """
                SELECT count(*)
                FROM solution_pattern sp
                WHERE EXISTS (
                        SELECT 1 FROM pattern_source ps
                        JOIN document d ON d.id = ps.document_id
                        WHERE ps.pattern_id = sp.id AND d.document_type IN (:t, :legacy)
                      )
                  AND NOT EXISTS (
                        SELECT 1 FROM pattern_source ps2
                        JOIN document d2 ON d2.id = ps2.document_id
                        WHERE ps2.pattern_id = sp.id
                          AND d2.document_type NOT IN (:t, :legacy)
                      )
                """,
                t=TENDER_DOCUMENT_TYPE,
                legacy=LEGACY_TENDER_TYPE,
            ),
            # 既被 tender 也被非 tender 引用（合法可复用，不动）
            "mixed_source_patterns": _scalar(
                session,
                """
                SELECT count(*)
                FROM solution_pattern sp
                WHERE EXISTS (
                        SELECT 1 FROM pattern_source ps
                        JOIN document d ON d.id = ps.document_id
                        WHERE ps.pattern_id = sp.id AND d.document_type IN (:t, :legacy)
                      )
                  AND EXISTS (
                        SELECT 1 FROM pattern_source ps2
                        JOIN document d2 ON d2.id = ps2.document_id
                        WHERE ps2.pattern_id = sp.id
                          AND d2.document_type NOT IN (:t, :legacy)
                      )
                """,
                t=TENDER_DOCUMENT_TYPE,
                legacy=LEGACY_TENDER_TYPE,
            ),
            "tender_only_already_quarantined": _scalar(
                session,
                """
                SELECT count(*)
                FROM solution_pattern sp
                WHERE sp.status = :q
                  AND EXISTS (
                        SELECT 1 FROM pattern_source ps
                        JOIN document d ON d.id = ps.document_id
                        WHERE ps.pattern_id = sp.id AND d.document_type IN (:t, :legacy)
                      )
                  AND NOT EXISTS (
                        SELECT 1 FROM pattern_source ps2
                        JOIN document d2 ON d2.id = ps2.document_id
                        WHERE ps2.pattern_id = sp.id
                          AND d2.document_type NOT IN (:t, :legacy)
                      )
                """,
                q=QUARANTINED_STATUS,
                t=TENDER_DOCUMENT_TYPE,
                legacy=LEGACY_TENDER_TYPE,
            ),
            "active_patterns_total": _scalar(
                session,
                "SELECT count(*) FROM solution_pattern WHERE status = 'active'",
            ),
        }
    return stats


def _apply() -> dict[str, int]:
    """执行收敛与隔离（幂等）。"""
    results: dict[str, int] = {}

    with session_scope() as session:
        # 1. 类型收敛 tender_doc → tender
        results["normalized_documents"] = (
            session.execute(
                text(
                    "UPDATE document SET document_type = :new_type, updated_at = CURRENT_TIMESTAMP "
                    "WHERE document_type = :old_type"
                ),
                {"new_type": TENDER_DOCUMENT_TYPE, "old_type": LEGACY_TENDER_TYPE},
            ).rowcount
            or 0
        )

        # 2. 隔离 tender-only pattern（可逆，不删行）
        results["quarantined_patterns"] = (
            session.execute(
                text(
                    """
                    UPDATE solution_pattern sp
                    SET status = :q, updated_at = CURRENT_TIMESTAMP
                    WHERE sp.status <> :q
                      AND EXISTS (
                            SELECT 1 FROM pattern_source ps
                            JOIN document d ON d.id = ps.document_id
                            WHERE ps.pattern_id = sp.id AND d.document_type = :t
                          )
                      AND NOT EXISTS (
                            SELECT 1 FROM pattern_source ps2
                            JOIN document d2 ON d2.id = ps2.document_id
                            WHERE ps2.pattern_id = sp.id
                              AND d2.document_type <> :t
                          )
                    """
                ),
                {"q": QUARANTINED_STATUS, "t": TENDER_DOCUMENT_TYPE},
            ).rowcount
            or 0
        )

    return results


def _verify() -> list[str]:
    """复核：返回仍然违规的项（应为空）。"""
    problems: list[str] = []
    with session_scope() as session:
        legacy = _scalar(
            session,
            "SELECT count(*) FROM document WHERE document_type = :t",
            t=LEGACY_TENDER_TYPE,
        )
        if legacy:
            problems.append(f"仍有 {legacy} 条 document.document_type = '{LEGACY_TENDER_TYPE}'")

        # 隔离后，仍然「active 且来源全是 tender」的 pattern 应为 0
        leaked = _scalar(
            session,
            """
            SELECT count(*)
            FROM solution_pattern sp
            WHERE sp.status = 'active'
              AND EXISTS (
                    SELECT 1 FROM pattern_source ps
                    JOIN document d ON d.id = ps.document_id
                    WHERE ps.pattern_id = sp.id AND d.document_type = :t
                  )
              AND NOT EXISTS (
                    SELECT 1 FROM pattern_source ps2
                    JOIN document d2 ON d2.id = ps2.document_id
                    WHERE ps2.pattern_id = sp.id AND d2.document_type <> :t
                  )
            """,
            t=TENDER_DOCUMENT_TYPE,
        )
        if leaked:
            problems.append(f"仍有 {leaked} 个 active 且仅由 tender 支撑的 pattern 未被隔离")

        # QA 白名单里不该出现 tender
        if TENDER_DOCUMENT_TYPE in QA_DOCUMENT_TYPES:
            problems.append("QA_DOCUMENT_TYPES 白名单里出现了 tender（代码配置错误）")

    return problems


def _print(stats: dict[str, int]) -> None:
    print("\n=== tender 数据普查 ===")
    for key, value in stats.items():
        print(f"  {key:34s} = {value}")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="招标文件来源数据收敛与隔离（§4.4）")
    parser.add_argument(
        "--apply", action="store_true", help="执行收敛与隔离（默认只读普查）"
    )
    args = parser.parse_args()

    before = _census()
    _print(before)

    if not args.apply:
        print(
            "\n[DRY-RUN] 未写库。将执行：\n"
            f"  1) document.document_type '{LEGACY_TENDER_TYPE}' → '{TENDER_DOCUMENT_TYPE}'"
            f"（{before['document_tender_legacy']} 条）\n"
            f"  2) 把 {before['tender_only_patterns']} 个「仅由 tender 支撑」的 pattern "
            f"置为 status='{QUARANTINED_STATUS}'（可逆，不删行）\n"
            f"  3) 混合来源 pattern（{before['mixed_source_patterns']} 个）不动 —— "
            "非 tender 来源仍合法，代码侧过滤已排除 tender 那一侧\n"
            "\n加 --apply 执行。"
        )
        return

    results = _apply()
    print("\n=== 执行结果 ===")
    for key, value in results.items():
        print(f"  {key:34s} = {value}")

    after = _census()
    _print(after)

    problems = _verify()
    if problems:
        print("\n[FAIL] 复核未通过：")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)
    print("\n[PASS] 复核通过：无遗留 tender_doc，无未隔离的 tender-only pattern。")


if __name__ == "__main__":
    main()
