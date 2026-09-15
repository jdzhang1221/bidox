"""索引生命周期守卫的幂等迁移（批次 A / §4.3）。

`init_db()` 用的 `Base.metadata.create_all` 只会**新建**缺失的表，不会给已存在的表加列。
本脚本补齐 `document_section` / `document_chunk` / `pattern_source` 的 `index_version` 列，
建 `document_index_guard` / `document_pattern_job` 与配套索引，并为存量 document 回填 guard。

用法::

    uv run python scripts/migrate_index_guard.py              # 只读检查（默认）
    uv run python scripts/migrate_index_guard.py --apply      # 执行迁移

回填语义（重要）
----------------
存量 document 的 guard 回填为 `lifecycle_status='ACTIVE'`、`current_index_version=NULL`、
`index_input_hash=NULL`。`NULL` 表示「版本未知」——下一次显式 ingest 会被判定为 `APPLY`（首次），
从而能正常接替历史索引；若回填成某个具体版本号，反而会让新解析被误判成
「同版本不同输入」而拒绝。`tenant_id IS NULL` 的 document **跳过**（§4.1 规则：不猜归属）。
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.core.database import session_scope
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# DDL（全部幂等）
# --------------------------------------------------------------------------- #
DDL_STATEMENTS: list[tuple[str, str]] = [
    (
        "document_section.index_version",
        "ALTER TABLE document_section ADD COLUMN IF NOT EXISTS index_version bigint",
    ),
    (
        "document_chunk.index_version",
        "ALTER TABLE document_chunk ADD COLUMN IF NOT EXISTS index_version bigint",
    ),
    (
        "pattern_source.index_version",
        "ALTER TABLE pattern_source ADD COLUMN IF NOT EXISTS index_version bigint",
    ),
    (
        "document_index_guard 表",
        """
        CREATE TABLE IF NOT EXISTS document_index_guard (
            document_id           bigint PRIMARY KEY,
            tenant_id             bigint NOT NULL,
            lifecycle_status      varchar(16) NOT NULL DEFAULT 'ACTIVE',
            current_index_version bigint,
            index_input_hash      char(64),
            updated_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_document_index_guard_lifecycle
                CHECK (lifecycle_status IN ('ACTIVE', 'DELETED', 'QUARANTINED'))
        )
        """,
    ),
    (
        "document_pattern_job 表",
        """
        CREATE TABLE IF NOT EXISTS document_pattern_job (
            tenant_id     bigint NOT NULL,
            document_id   bigint NOT NULL,
            index_version bigint NOT NULL,
            input_hash    char(64) NOT NULL,
            state         varchar(16) NOT NULL,
            owner_id      uuid,
            expires_at    timestamptz,
            error_message varchar(1024),
            updated_at    timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, document_id, index_version),
            CONSTRAINT ck_document_pattern_job_state
                CHECK (state IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'OBSOLETE'))
        )
        """,
    ),
    (
        "idx_document_index_guard_tenant_lifecycle",
        "CREATE INDEX IF NOT EXISTS idx_document_index_guard_tenant_lifecycle"
        " ON document_index_guard (tenant_id, lifecycle_status)",
    ),
    (
        "idx_document_pattern_job_live",
        "CREATE INDEX IF NOT EXISTS idx_document_pattern_job_live"
        " ON document_pattern_job (tenant_id, document_id)"
        " WHERE state IN ('PENDING', 'RUNNING')",
    ),
    (
        "idx_document_section_version",
        "CREATE INDEX IF NOT EXISTS idx_document_section_version"
        " ON document_section (document_id, index_version)",
    ),
    (
        "idx_document_chunk_version",
        "CREATE INDEX IF NOT EXISTS idx_document_chunk_version"
        " ON document_chunk (document_id, index_version)",
    ),
    (
        "idx_pattern_source_version",
        "CREATE INDEX IF NOT EXISTS idx_pattern_source_version"
        " ON pattern_source (document_id, index_version)",
    ),
]

# 存量回填：current_index_version / index_input_hash 保持 NULL（= 版本未知，下次 ingest 走 APPLY）
BACKFILL_GUARD = """
INSERT INTO document_index_guard
    (document_id, tenant_id, lifecycle_status, current_index_version, index_input_hash)
SELECT d.id, d.tenant_id, 'ACTIVE', NULL, NULL
FROM document d
WHERE d.tenant_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM document_index_guard g WHERE g.document_id = d.id)
"""

# 需要人工处理的存量（不自动补归属）
PENDING_MANUAL: list[tuple[str, str]] = [
    (
        "document.tenant_id IS NULL（无法回填 guard，需人工确认归属）",
        "select count(*) from document where tenant_id is null",
    ),
    (
        "document 有行但 guard 缺失（回填失败）",
        "select count(*) from document d"
        " where d.tenant_id is not null"
        "   and not exists (select 1 from document_index_guard g where g.document_id = d.id)",
    ),
]

INVARIANTS: list[tuple[str, str]] = [
    (
        "guard.tenant_id 与其 document 不一致",
        "select count(*) from document_index_guard g join document d on d.id = g.document_id"
        " where g.tenant_id is distinct from d.tenant_id",
    ),
    (
        "guard 指向不存在的 document（孤儿 guard）",
        "select count(*) from document_index_guard g"
        " left join document d on d.id = g.document_id where d.id is null",
    ),
    (
        "guard 的 document_id 落在多个租户",
        "select count(*) from (select document_id from document_index_guard"
        " group by document_id having count(*) > 1) t",
    ),
]


def _has_column(session, table: str, column: str) -> bool:
    return bool(
        session.execute(
            text(
                "select count(*) from information_schema.columns"
                " where table_name = :t and column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_table(session, table: str) -> bool:
    return bool(
        session.execute(
            text("select count(*) from information_schema.tables where table_name = :t"),
            {"t": table},
        ).scalar()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="索引生命周期守卫迁移")
    parser.add_argument("--apply", action="store_true", help="执行迁移（不加则只读检查）")
    args = parser.parse_args()

    setup_logging()
    print("=" * 78)
    print(f"索引生命周期守卫迁移 mode={'APPLY' if args.apply else 'READ-ONLY'}")
    print("=" * 78)

    with session_scope() as session:
        # --- 迁移前状态 ---
        print("\n=== 一、迁移前状态 ===")
        for table, column in (
            ("document_section", "index_version"),
            ("document_chunk", "index_version"),
            ("pattern_source", "index_version"),
        ):
            print(f"  {table}.{column:14} 存在={_has_column(session, table, column)}")
        for table in ("document_index_guard", "document_pattern_job"):
            print(f"  {table:26} 存在={_has_table(session, table)}")

        # --- 执行 DDL ---
        print(f"\n=== 二、DDL（{'执行' if args.apply else '未执行'}）===")
        for name, ddl in DDL_STATEMENTS:
            if args.apply:
                session.execute(text(ddl))
                print(f"  [OK] {name}")
            else:
                print(f"  [待执行] {name}")

        # --- 回填 ---
        print(f"\n=== 三、存量 guard 回填（{'执行' if args.apply else '未执行'}）===")
        guard_exists = _has_table(session, "document_index_guard")
        if not guard_exists:
            print("  [SKIP] guard 表尚不存在，先执行 DDL（--apply）后再回填")
        else:
            pending = session.execute(
                text(
                    "select count(*) from document d where d.tenant_id is not null"
                    " and not exists"
                    " (select 1 from document_index_guard g where g.document_id = d.id)"
                )
            ).scalar()
            print(
                f"  待回填 {pending} 行"
                "（current_index_version / index_input_hash 置 NULL = 版本未知）"
            )
            if args.apply:
                session.execute(text(BACKFILL_GUARD))
                after = session.execute(
                    text(
                        "select count(*) from document d where d.tenant_id is not null"
                        " and not exists"
                        " (select 1 from document_index_guard g where g.document_id = d.id)"
                    )
                ).scalar()
                print(f"  回填后剩余待处理 {after} 行")

        # --- 迁移后复核 ---
        print("\n=== 四、迁移后复核 ===")
        for table, column in (
            ("document_section", "index_version"),
            ("document_chunk", "index_version"),
            ("pattern_source", "index_version"),
        ):
            print(f"  {table}.{column:14} 存在={_has_column(session, table, column)}")
        for table in ("document_index_guard", "document_pattern_job"):
            print(f"  {table:26} 存在={_has_table(session, table)}")

        if _has_table(session, "document_index_guard"):
            print(
                "  guard 行数 = "
                f"{session.execute(text('select count(*) from document_index_guard')).scalar()}"
            )

        print("\n=== 五、一致性不变量（全部应为 0）===")
        broken = []
        for name, sql in INVARIANTS:
            if not _has_table(session, "document_index_guard"):
                print(f"  [SKIP] {name}（guard 表尚不存在）")
                continue
            cnt = session.execute(text(sql)).scalar() or 0
            flag = "OK" if cnt == 0 else "!!"
            print(f"  [{flag}] {name:44} {cnt}")
            if cnt:
                broken.append((name, cnt))

        print("\n=== 六、需人工处理 ===")
        for name, sql in PENDING_MANUAL:
            if "document_index_guard" in sql and not _has_table(session, "document_index_guard"):
                print(f"  [SKIP] {name}")
                continue
            print(f"  {name:52} {session.execute(text(sql)).scalar() or 0}")

    print("\n" + "=" * 78)
    if broken:
        print(f"[WARN] {len(broken)} 项不变量违规，需人工确认：")
        for name, cnt in broken:
            print(f"       - {name} = {cnt}")
    elif args.apply:
        print("[PASS] 迁移完成，不变量全部为 0")
    else:
        print("[INFO] 只读模式，未修改任何数据。加 --apply 执行迁移。")
    print("=" * 78)


if __name__ == "__main__":
    main()
