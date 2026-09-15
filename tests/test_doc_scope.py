"""默认问答文档范围与 tender 隔离回归测试（批次 A / §4.4）。

覆盖四层，逐层收紧：

1. **常量与纯函数** —— QA 白名单恒不含 tender；显式传 tender 也不生效（只收紧不放宽）；
2. **SQL 层** —— pattern 召回与 fingerprint 回查都带「来源文档类型」条件，
   不再只过滤 `solution_pattern.tenant_id`；
3. **持久化层** —— 非白名单类型（tender）在 `SolutionPatternIndexer.extract/save`
   与 `_spawn_pattern_extraction` 处被强制拒绝，且**早于** LLM 调用与任何 DB 访问；
4. **编排层** —— `ingest_result` 对 tender 强制关掉 pattern 抽取，
   即使调用方传了 `with_patterns=True`。

不依赖数据库：用假 session 捕获 SQLAlchemy 语句，再编译成 PostgreSQL SQL 断言。
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.doc_types import (
    ALL_DOCUMENT_TYPES,
    LEGACY_DOCUMENT_TYPE_ALIASES,
    PATTERN_ELIGIBLE_DOCUMENT_TYPES,
    QA_DOCUMENT_TYPES,
    TENDER_DOCUMENT_TYPE,
    is_pattern_eligible,
    is_tender,
    normalize_document_type,
    pattern_document_types,
    resolve_qa_document_types,
)
from app.models.pattern import pattern_source_type_clause


# --------------------------------------------------------------------------- #
# 测试脚手架
# --------------------------------------------------------------------------- #
class _EmptyResult:
    def all(self) -> list[Any]:
        return []

    def scalars(self) -> "_EmptyResult":
        return self

    rowcount = 1


class _CapturingSession:
    """记录 execute() 收到的语句，返回空结果集；满足 _ensure_document 的最小接口。"""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.info: dict[str, Any] = {}

    def execute(self, stmt: Any) -> _EmptyResult:
        self.statements.append(stmt)
        return _EmptyResult()

    def get(self, model: Any, pk: Any) -> Any:
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None


def _patch_session_scope(monkeypatch: pytest.MonkeyPatch, module: Any) -> _CapturingSession:
    session = _CapturingSession()

    @contextlib.contextmanager
    def scope():
        yield session

    monkeypatch.setattr(module, "session_scope", scope)
    return session


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _where_sql(stmt: Any) -> str:
    sql = _sql(stmt)
    assert "WHERE" in sql, f"语句没有 WHERE 子句:\n{sql}"
    return sql.split("WHERE", 1)[1]


def _in_params(stmt: Any) -> list[Any]:
    """取出编译后所有绑定参数的值（含 POSTCOMPILE 展开前的列表）。"""
    compiled = stmt.compile(dialect=postgresql.dialect())
    return list(compiled.params.values())


def _flatten(values: list[Any]) -> list[Any]:
    flat: list[Any] = []
    for v in values:
        flat.extend(v if isinstance(v, (list, tuple, set)) else [v])
    return flat


def _assert_tender_not_reachable(stmt: Any) -> None:
    """断言 tender 不会出现在任何绑定参数里。"""
    flat = _flatten(_in_params(stmt))
    assert TENDER_DOCUMENT_TYPE not in flat, f"tender 出现在绑定参数里：{flat}"


# --------------------------------------------------------------------------- #
# 1. 常量与纯函数
# --------------------------------------------------------------------------- #
def test_qa_whitelist_excludes_tender() -> None:
    assert TENDER_DOCUMENT_TYPE == "tender"
    assert TENDER_DOCUMENT_TYPE not in QA_DOCUMENT_TYPES
    assert set(QA_DOCUMENT_TYPES) == set(ALL_DOCUMENT_TYPES) - {TENDER_DOCUMENT_TYPE}
    # 计划 §4.4 列出的八类
    assert set(QA_DOCUMENT_TYPES) == {
        "historical_bid",
        "enterprise_profile",
        "qualification",
        "project_case",
        "product_manual",
        "technical_document",
        "regulation",
        "other",
    }


def test_pattern_eligible_types_are_the_qa_whitelist() -> None:
    assert PATTERN_ELIGIBLE_DOCUMENT_TYPES == QA_DOCUMENT_TYPES


def test_is_tender() -> None:
    assert is_tender("tender") is True
    assert is_tender(" tender ") is True
    assert is_tender("historical_bid") is False
    assert is_tender(None) is False
    assert is_tender("") is False


@pytest.mark.parametrize(
    "document_type",
    [t for t in QA_DOCUMENT_TYPES],
)
def test_is_pattern_eligible_accepts_whitelist(document_type: str) -> None:
    assert is_pattern_eligible(document_type) is True


@pytest.mark.parametrize(
    "document_type",
    ["tender", "tender_doc", None, "", "   ", "unknown_type", "HISTORICAL_BID"],
)
def test_is_pattern_eligible_rejects_everything_else(document_type) -> None:
    """未知类型按「不允许」处理：宁可少抽取，也不让来历不明的类型进方案库。"""
    assert is_pattern_eligible(document_type) is False


def test_legacy_tender_doc_is_aliased_to_tender() -> None:
    """`tender_doc` 必须映射成 tender，绝不能落到 normalize 的默认值。

    否则存量招标文件会被静默重分类成 historical_bid —— 那正好是 §4.4 要消除的混淆，
    而且会让「按类型排除招标文件」的过滤在存量数据上继续失效。
    """
    assert LEGACY_DOCUMENT_TYPE_ALIASES["tender_doc"] == TENDER_DOCUMENT_TYPE
    assert normalize_document_type("tender_doc") == TENDER_DOCUMENT_TYPE
    assert is_tender("tender_doc") is True
    assert is_pattern_eligible("tender_doc") is False
    assert resolve_qa_document_types(["tender_doc"]) == list(QA_DOCUMENT_TYPES)


@pytest.mark.parametrize("value", [None, [], ()])
def test_resolve_qa_document_types_defaults_to_whitelist(value) -> None:
    assert resolve_qa_document_types(value) == list(QA_DOCUMENT_TYPES)


def test_resolve_qa_document_types_returns_declaration_order() -> None:
    """返回顺序恒为 ALL_DOCUMENT_TYPES 声明顺序，保证 SQL 参数稳定可比对。"""
    result = resolve_qa_document_types(["regulation", "historical_bid"])
    assert result == ["historical_bid", "regulation"]


def test_resolve_qa_document_types_cannot_widen_to_tender() -> None:
    """显式传 tender 也不生效 —— 只收紧，不放宽。"""
    assert resolve_qa_document_types(["tender"]) == list(QA_DOCUMENT_TYPES)
    assert resolve_qa_document_types(["tender", "tender"]) == list(QA_DOCUMENT_TYPES)


def test_resolve_qa_document_types_intersects_with_whitelist() -> None:
    assert resolve_qa_document_types(["qualification", "tender"]) == ["qualification"]
    assert resolve_qa_document_types(["qualification", "regulation"]) == [
        "qualification",
        "regulation",
    ]


def test_resolve_qa_document_types_falls_back_on_all_unknown() -> None:
    """全是未知类型时回退为白名单，而不是退化成「不过滤」。"""
    assert resolve_qa_document_types(["nope", "nada"]) == list(QA_DOCUMENT_TYPES)


def test_pattern_document_types_never_contains_tender() -> None:
    assert TENDER_DOCUMENT_TYPE not in pattern_document_types()
    assert TENDER_DOCUMENT_TYPE not in pattern_document_types(["tender"])
    assert TENDER_DOCUMENT_TYPE not in pattern_document_types(["tender_doc"])


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("tender", "tender"),
        (" tender ", "tender"),
        ("historical_bid", "historical_bid"),
        ("tender_doc", "tender"),
        ("unknown", "historical_bid"),
        (None, "historical_bid"),
        ("", "historical_bid"),
    ],
)
def test_normalize_document_type(raw, expected: str) -> None:
    assert normalize_document_type(raw) == expected


def test_normalize_document_type_honours_custom_default() -> None:
    assert normalize_document_type(None, default="other") == "other"


# --------------------------------------------------------------------------- #
# 2. SQL 层：来源文档类型是硬条件
# --------------------------------------------------------------------------- #
def test_clause_requires_a_source_document_of_allowed_type() -> None:
    clause = pattern_source_type_clause(["historical_bid", "qualification"])
    sql = _sql(clause)

    assert "EXISTS" in sql
    # 必须真的 JOIN 到 document 并比对类型，而不是只看 pattern 自身
    assert "pattern_source" in sql
    assert "document" in sql
    assert "pattern_source.pattern_id = solution_pattern.id" in sql
    assert "document.document_type IN " in sql

    values = _flatten(_in_params(clause))
    assert "historical_bid" in values
    assert "qualification" in values
    assert TENDER_DOCUMENT_TYPE not in values


def test_clause_defaults_to_qa_whitelist_when_types_omitted() -> None:
    """忘记传参时的默认行为必须是安全的：仍然排除 tender。"""
    sql = _sql(pattern_source_type_clause())
    assert "document.document_type IN " in sql
    values = _flatten(_in_params(pattern_source_type_clause()))
    assert TENDER_DOCUMENT_TYPE not in values
    assert "historical_bid" in values


def test_clause_with_empty_types_falls_back_to_whitelist() -> None:
    """空白名单与未传等价（由 resolve 归一化），不会退化成「不过滤」。"""
    sql = _sql(pattern_source_type_clause([]))
    assert "document.document_type IN " in sql
    values = _flatten(_in_params(pattern_source_type_clause([])))
    assert TENDER_DOCUMENT_TYPE not in values


def test_pattern_retriever_scopes_to_source_document_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.retrieval.pattern as module

    session = _patch_session_scope(monkeypatch, module)
    module.PatternRetriever().search([0.1] * 8, tenant_id=42, top_k=3)

    assert len(session.statements) == 1
    stmt = session.statements[0]
    where = _where_sql(stmt)
    # 租户条件仍在
    assert "solution_pattern.tenant_id = " in where
    # 新增：来源文档类型条件
    assert "document.document_type IN " in where, f"pattern 召回缺少来源类型过滤:\n{where}"
    _assert_tender_not_reachable(stmt)


def test_pattern_retriever_cannot_be_talked_into_tender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.retrieval.pattern as module

    session = _patch_session_scope(monkeypatch, module)
    module.PatternRetriever().search(
        [0.1] * 8, tenant_id=42, top_k=3, document_types=["tender"]
    )

    stmt = session.statements[0]
    assert "document.document_type IN " in _where_sql(stmt)
    _assert_tender_not_reachable(stmt)


def test_dedupe_patterns_scopes_lookup_to_source_document_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fingerprint 回查必须同时带租户与来源类型，否则同租户的 tender pattern 会被聚合进来。"""
    import app.knowledge.service as module

    session = _patch_session_scope(monkeypatch, module)
    patterns = [{"pattern_id": 1, "fingerprint": "fp-1", "sources": []}]

    module.KnowledgeService._dedupe_patterns(patterns, tenant_id=42, knowledge_base_id=1001)

    assert session.statements, "应按 fingerprint 聚合一次"
    stmt = session.statements[0]
    where = _where_sql(stmt)
    assert "solution_pattern.fingerprint IN " in where
    assert "solution_pattern.tenant_id = " in where
    assert "document.document_type IN " in where, f"fingerprint 回查缺少来源类型过滤:\n{where}"
    _assert_tender_not_reachable(stmt)


# --------------------------------------------------------------------------- #
# 3. 持久化/调度层：非白名单类型强制拒绝
# --------------------------------------------------------------------------- #
class _Bomb:
    """一旦被调用就报错 —— 用来证明某段代码没有执行。"""

    def __init__(self, message: str) -> None:
        self.message = message

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(self.message)


def _bare_pattern_indexer() -> Any:
    from app.pattern.indexer import SolutionPatternIndexer

    indexer = SolutionPatternIndexer.__new__(SolutionPatternIndexer)
    indexer._embedding = _Bomb("tender 文档不得调用 embedding")
    indexer._extractor = _Bomb("tender 文档不得调用 LLM 抽取")
    return indexer


def test_pattern_extract_returns_empty_for_tender_without_calling_llm() -> None:
    indexer = _bare_pattern_indexer()

    class _Result:
        sections: list[Any] = []

    assert indexer.extract(_Result(), document_type="tender") == []


def test_pattern_save_discards_tender_without_calling_embedding() -> None:
    indexer = _bare_pattern_indexer()
    pairs = [(object(), object())]

    count = indexer.save(
        pairs,
        id_map={},
        document_id=1,
        tenant_id=1,
        document_type="tender",
    )
    assert count == 0


def test_pattern_save_discards_legacy_tender_doc_type() -> None:
    """历史遗留值 tender_doc 会被归一化成 tender，因此同样被拒绝。"""
    indexer = _bare_pattern_indexer()
    assert indexer.save(
        [(object(), object())],
        id_map={},
        document_id=1,
        tenant_id=1,
        document_type="tender_doc",
    ) == 0


def test_spawn_pattern_extraction_returns_before_touching_db_for_tender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守卫必须在抢 pattern job 之前返回，否则会为注定丢弃的文档占用租约。"""
    import app.knowledge.ingest as module

    @contextlib.contextmanager
    def boom():
        raise AssertionError("非白名单类型不得访问数据库")
        yield  # pragma: no cover

    monkeypatch.setattr(module, "session_scope", boom)

    class _Result:
        sections: list[Any] = []

    module._spawn_pattern_extraction(
        _Result(),
        {},
        document_id=1,
        index_version=1,
        input_hash="h",
        tenant_id=1,
        document_type="tender",
    )


# --------------------------------------------------------------------------- #
# 4. 编排层：ingest_result 对 tender 强制关掉 pattern 抽取
# --------------------------------------------------------------------------- #
class _FakeIndexer:
    def embed_chunks(self, chunks: Any) -> list[Any]:
        return []

    def index_document(self, result: Any, document_id: int, **kwargs: Any) -> dict[str, Any]:
        return {"id_map": {}, "section_count": 0, "chunk_count": 0}


class _FakeParseResult:
    def __init__(self) -> None:
        self.chunks: list[Any] = []
        self.sections: list[Any] = []


def _run_ingest_for_type(
    monkeypatch: pytest.MonkeyPatch, *, document_type: str, with_patterns: bool
) -> list[dict[str, Any]]:
    """跑一遍 ingest_result（外部依赖全部打桩），返回 pattern 抽取的调用记录。"""
    import app.knowledge.ingest as module
    from app.knowledge.guard import IndexDecision, GuardState

    session = _patch_session_scope(monkeypatch, module)
    spawned: list[dict[str, Any]] = []

    monkeypatch.setattr(module, "add_vector_extension", lambda: None)
    monkeypatch.setattr(module, "init_db", lambda: None)
    monkeypatch.setattr(module, "KnowledgeIndexer", _FakeIndexer)
    monkeypatch.setattr(
        module,
        "begin_index",
        lambda s, **kwargs: (
            GuardState(
                document_id=kwargs["document_id"],
                tenant_id=kwargs["tenant_id"],
                lifecycle_status="ACTIVE",
                current_index_version=None,
                index_input_hash=None,
            ),
            IndexDecision.APPLY,
        ),
    )
    monkeypatch.setattr(module, "commit_index", lambda s, **kwargs: None)
    monkeypatch.setattr(
        module,
        "_spawn_pattern_extraction",
        lambda result, id_map, **kwargs: spawned.append(kwargs),
    )

    module.ingest_result(
        _FakeParseResult(),
        document_id=1,
        tenant_id=1,
        parse_log_id=1,
        document_type=document_type,
        with_patterns=with_patterns,
    )
    assert session.statements, "应至少写入一次"
    return spawned


def test_ingest_result_spawns_patterns_for_historical_bid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = _run_ingest_for_type(
        monkeypatch, document_type="historical_bid", with_patterns=True
    )
    assert len(spawned) == 1
    assert spawned[0]["document_type"] == "historical_bid"


def test_ingest_result_forces_patterns_off_for_tender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使调用方传 with_patterns=True，持久化层也会把它改掉。"""
    spawned = _run_ingest_for_type(monkeypatch, document_type="tender", with_patterns=True)
    assert spawned == [], "tender 文档不得触发 pattern 抽取"


def test_ingest_result_respects_explicit_with_patterns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = _run_ingest_for_type(
        monkeypatch, document_type="historical_bid", with_patterns=False
    )
    assert spawned == []
