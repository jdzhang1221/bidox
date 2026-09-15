"""租户隔离回归测试(批次 A / P0)。

覆盖三层:
1. 请求 schema —— tenantId/tenant_id 必填,缺失、null、冲突一律拒绝;
2. 守卫函数 —— 漏传时抛错而不是静默退化成全库检索;
3. 真实 SQL —— 三路检索都带租户条件,章节扩展被限制在可信 document_id 内,
   fingerprint 去重不再回查全库。

不依赖数据库:用假 session 捕获 SQLAlchemy 语句,再编译成 PostgreSQL SQL 断言。
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.schemas import (
    GenerateRequest,
    LocalParseRequest,
    ParseRequest,
    PatternSearchRequest,
    RagRequest,
    SearchRequest,
)
from app.core.tenant import (
    TenantScopeError,
    require_allowed_document_ids,
    require_tenant_id,
)


# --------------------------------------------------------------------------- #
# 测试脚手架:假 session + 捕获 SQL
# --------------------------------------------------------------------------- #
class _EmptyResult:
    def all(self) -> list[Any]:
        return []

    def scalars(self) -> "_EmptyResult":
        return self


class _CapturingSession:
    """记录 execute() 收到的语句,返回空结果集。"""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, stmt: Any) -> _EmptyResult:
        self.statements.append(stmt)
        return _EmptyResult()

    def get(self, model: Any, pk: Any) -> Any:  # pragma: no cover - 由子类覆盖
        raise AssertionError("本用例不应调用 session.get")


def _patch_session_scope(monkeypatch: pytest.MonkeyPatch, module: Any) -> _CapturingSession:
    """把模块里的 session_scope 换成返回同一个捕获型 session 的上下文管理器。"""
    session = _CapturingSession()

    @contextlib.contextmanager
    def scope():
        yield session

    monkeypatch.setattr(module, "session_scope", scope)
    return session


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _where_sql(stmt: Any) -> str:
    """只取 WHERE 之后的部分(实体查询会把所有列都 SELECT 出来,不能整串断言)。"""
    sql = _sql(stmt)
    assert "WHERE" in sql, f"语句没有 WHERE 子句:\n{sql}"
    return sql.split("WHERE", 1)[1]


def _tenant_params(stmt: Any) -> list[Any]:
    """取出编译后所有名字以 tenant_id 开头的绑定参数值。"""
    compiled = stmt.compile(dialect=postgresql.dialect())
    return [v for k, v in compiled.params.items() if k.startswith("tenant_id")]


def _assert_tenant_scoped(stmt: Any, column: str, tenant_id: int) -> None:
    where = _where_sql(stmt)
    assert f"{column} = " in where, f"WHERE 缺少租户条件:\n{where}"
    values = _tenant_params(stmt)
    assert values, f"SQL 没有绑定 tenant 参数:\n{where}"
    assert all(v == tenant_id for v in values), f"租户参数不是 {tenant_id}: {values}"


# --------------------------------------------------------------------------- #
# 1. 请求 schema
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "build",
    [
        lambda: SearchRequest(query="q"),
        lambda: PatternSearchRequest(query="q"),
        lambda: GenerateRequest(query="q"),
        lambda: RagRequest(query="q"),
    ],
    ids=["SearchRequest", "PatternSearchRequest", "GenerateRequest", "RagRequest"],
)
def test_tenant_is_required_on_search_and_generation_requests(build) -> None:
    with pytest.raises(ValidationError):
        build()


@pytest.mark.parametrize(
    "model,payload",
    [
        (SearchRequest, {"query": "q"}),
        (RagRequest, {"query": "q"}),
        (ParseRequest, {"documentId": 1, "persist": True}),
        (LocalParseRequest, {"path": "/tmp/a.docx", "persist": True}),
    ],
)
def test_tenant_must_not_be_null(model, payload) -> None:
    with pytest.raises(ValidationError):
        model(**payload, tenantId=None)


@pytest.mark.parametrize(
    "model,payload",
    [
        (SearchRequest, {"query": "q"}),
        (RagRequest, {"query": "q"}),
        (ParseRequest, {"documentId": 1, "persist": True}),
        (LocalParseRequest, {"path": "/tmp/a.docx", "persist": True}),
    ],
)
def test_camel_and_snake_tenant_are_both_accepted(model, payload) -> None:
    assert model(**payload, tenantId=11).tenant_id == 11
    assert model(**payload, tenant_id=11).tenant_id == 11
    # 两种写法同时出现且一致时不算冲突
    assert model(**payload, tenantId=11, tenant_id=11).tenant_id == 11


@pytest.mark.parametrize(
    "model,payload",
    [
        (SearchRequest, {"query": "q"}),
        (RagRequest, {"query": "q"}),
        (ParseRequest, {"documentId": 1, "persist": True}),
    ],
)
def test_conflicting_tenant_forms_are_rejected(model, payload) -> None:
    with pytest.raises(ValidationError):
        model(**payload, tenantId=1, tenant_id=2)


def test_parse_request_without_persist_may_omit_tenant() -> None:
    """纯解析(不落库)不产生索引数据,允许不带租户。"""
    assert ParseRequest(documentId=1).tenant_id is None
    assert LocalParseRequest(path="/tmp/a.docx").tenant_id is None


# --------------------------------------------------------------------------- #
# 2. 守卫函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [None, 0, -1, "1", True])
def test_require_tenant_id_rejects_missing_or_invalid(bad) -> None:
    with pytest.raises(TenantScopeError):
        require_tenant_id(bad, where="test")


def test_require_tenant_id_returns_valid_value() -> None:
    assert require_tenant_id(7, where="test") == 7


def test_require_allowed_document_ids_rejects_missing() -> None:
    with pytest.raises(TenantScopeError):
        require_allowed_document_ids(None, where="test")


def test_require_allowed_document_ids_normalises_and_drops_none() -> None:
    assert require_allowed_document_ids([1, None, 2, 2], where="test") == {1, 2}
    assert require_allowed_document_ids([], where="test") == set()


def test_require_allowed_document_ids_rejects_bad_element() -> None:
    with pytest.raises(TenantScopeError):
        require_allowed_document_ids([1, "2"], where="test")


# --------------------------------------------------------------------------- #
# 3. 三路检索的 SQL 必须带租户条件
# --------------------------------------------------------------------------- #
def test_vector_retriever_enforces_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.retrieval.vector as module

    session = _patch_session_scope(monkeypatch, module)
    module.VectorRetriever().search(
        [0.1] * 8, tenant_id=42, top_k=3, document_types=["historical_bid"]
    )
    assert len(session.statements) == 1
    _assert_tenant_scoped(session.statements[0], "document_chunk.tenant_id", 42)
    # 不选知识库只是不加 knowledge_base_id 条件,租户条件仍在
    assert "knowledge_base_id" not in _where_sql(session.statements[0])


def test_keyword_retriever_enforces_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.retrieval.keyword as module

    session = _patch_session_scope(monkeypatch, module)
    module.KeywordRetriever().search("质量管控", tenant_id=42, top_k=3)
    assert len(session.statements) == 1
    _assert_tenant_scoped(session.statements[0], "document_chunk.tenant_id", 42)


def test_pattern_retriever_enforces_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.retrieval.pattern as module

    session = _patch_session_scope(monkeypatch, module)
    module.PatternRetriever().search([0.1] * 8, tenant_id=42, top_k=3)
    assert len(session.statements) == 1
    _assert_tenant_scoped(session.statements[0], "solution_pattern.tenant_id", 42)


@pytest.mark.parametrize(
    "call",
    [
        lambda: __import__(
            "app.retrieval.vector", fromlist=["VectorRetriever"]
        ).VectorRetriever().search([0.1] * 8, tenant_id=None),
        lambda: __import__(
            "app.retrieval.keyword", fromlist=["KeywordRetriever"]
        ).KeywordRetriever().search("q", tenant_id=None),
        lambda: __import__(
            "app.retrieval.pattern", fromlist=["PatternRetriever"]
        ).PatternRetriever().search([0.1] * 8, tenant_id=None),
    ],
    ids=["vector", "keyword", "pattern"],
)
def test_retrievers_refuse_missing_tenant_before_touching_db(
    monkeypatch: pytest.MonkeyPatch, call
) -> None:
    """守卫必须早于任何 DB 访问:session_scope 一旦被调用就报错,证明没走到那一步。"""
    for name in ("app.retrieval.vector", "app.retrieval.keyword", "app.retrieval.pattern"):
        module = __import__(name, fromlist=["session_scope"])

        @contextlib.contextmanager
        def boom():
            raise AssertionError("漏传租户时不得访问数据库")
            yield  # pragma: no cover

        monkeypatch.setattr(module, "session_scope", boom)
    with pytest.raises(TenantScopeError):
        call()


# --------------------------------------------------------------------------- #
# 4. HybridRetriever 逐层透传租户
# --------------------------------------------------------------------------- #
class _RecordingChannel:
    def __init__(self, **extra: Any) -> None:
        self.extra = extra
        self.calls: list[dict[str, Any]] = []

    def search(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append({"args": args, "kwargs": kwargs})
        return []


class _RecordingEmbedding:
    def embed_query(self, query: str) -> list[float]:
        return [0.0] * 8


def _bare_hybrid_retriever() -> Any:
    from app.retrieval.hybrid import HybridRetriever

    retriever = HybridRetriever.__new__(HybridRetriever)
    retriever._vector = _RecordingChannel()
    retriever._keyword = _RecordingChannel()
    retriever._pattern = _RecordingChannel()
    retriever._embedding = _RecordingEmbedding()
    retriever._reranker = None
    return retriever


def test_hybrid_search_passes_tenant_to_both_channels() -> None:
    retriever = _bare_hybrid_retriever()
    retriever.search("质量", tenant_id=9, rerank=False)

    assert retriever._vector.calls[0]["kwargs"]["tenant_id"] == 9
    assert retriever._keyword.calls[0]["kwargs"]["tenant_id"] == 9


def test_hybrid_search_patterns_passes_tenant_on_fallback_too() -> None:
    retriever = _bare_hybrid_retriever()
    # 首次按类型过滤返回空 → 走回退;回退也只能放宽类型,租户必须保留
    retriever.search_patterns("质量", tenant_id=9, pattern_types=["quality_system"], rerank=False)

    assert len(retriever._pattern.calls) == 2, "类型过滤为空时应回退为不过滤类型"
    for call in retriever._pattern.calls:
        assert call["kwargs"]["tenant_id"] == 9


def test_hybrid_retrieve_passes_tenant_to_both_paths() -> None:
    retriever = _bare_hybrid_retriever()
    seen: dict[str, Any] = {}

    def fake_search_patterns(query: str, tenant_id: int, **kwargs: Any) -> list[Any]:
        seen["patterns"] = tenant_id
        return []

    def fake_search(query: str, tenant_id: int, **kwargs: Any) -> list[Any]:
        seen["evidences"] = tenant_id
        return []

    retriever.search_patterns = fake_search_patterns
    retriever.search = fake_search
    retriever.retrieve("质量", tenant_id=9, pattern_types=["methodology"], rerank=False)

    assert seen == {"patterns": 9, "evidences": 9}


def test_hybrid_retrieve_refuses_missing_tenant() -> None:
    with pytest.raises(TenantScopeError):
        _bare_hybrid_retriever().retrieve("质量", tenant_id=None, pattern_types=["methodology"])


# --------------------------------------------------------------------------- #
# 5. 章节扩展限制在可信 document_id 内
# --------------------------------------------------------------------------- #
def test_section_expand_returns_empty_for_empty_allowed_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.retrieval.section as module

    session = _patch_session_scope(monkeypatch, module)
    assert module.SectionRetriever().expand([1, 2, 3], set()) == []
    assert session.statements == [], "空的可信文档集合不得访问数据库"


def test_section_expand_scopes_every_query_to_allowed_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.retrieval.section as module

    session = _patch_session_scope(monkeypatch, module)
    # 命中章节为空 → 只会执行第一条(命中)查询,但它本身也必须带 document_id 条件
    module.SectionRetriever().expand([1, 2], {100, 200})

    assert session.statements, "应至少执行一次章节查询"
    for stmt in session.statements:
        sql = _sql(stmt)
        assert "document_section.document_id IN " in sql, f"章节查询缺少可信文档限制:\n{sql}"


def test_section_expand_refuses_missing_allowed_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.retrieval.section as module

    _patch_session_scope(monkeypatch, module)
    with pytest.raises(TenantScopeError):
        module.SectionRetriever().expand([1], None)


def test_expand_pattern_sources_only_uses_pattern_own_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pattern_source 无 tenant 列,扩展范围只能是该 pattern 自己的 source 文档。"""
    import app.knowledge.service as module
    import app.retrieval.section as section_module

    captured: list[tuple[list[Any], Any]] = []

    class FakeSectionRetriever:
        def expand(self, section_ids, allowed_document_ids, document_id=None):
            captured.append((list(section_ids), allowed_document_ids))
            return []

    monkeypatch.setattr(section_module, "SectionRetriever", FakeSectionRetriever)
    monkeypatch.setattr(module, "SectionRetriever", FakeSectionRetriever)

    patterns = [
        {
            "pattern_id": 1,
            "sources": [
                {"document_id": 100, "section_id": 11},
                {"document_id": 200, "section_id": 22},
            ],
        }
    ]
    module.KnowledgeService._expand_pattern_sources(patterns)

    assert captured == [([11, 22], {100, 200})]


# --------------------------------------------------------------------------- #
# 6. fingerprint 去重不得跨租户回查
# --------------------------------------------------------------------------- #
def test_dedupe_patterns_scopes_fingerprint_lookup_to_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.knowledge.service as module

    session = _patch_session_scope(monkeypatch, module)
    patterns = [{"pattern_id": 1, "fingerprint": "fp-1", "sources": []}]

    module.KnowledgeService._dedupe_patterns(patterns, tenant_id=42, knowledge_base_id=1001)

    assert session.statements, "应按 fingerprint 聚合一次"
    stmt = session.statements[0]
    sql = _sql(stmt)
    assert "solution_pattern.fingerprint IN " in sql
    assert "solution_pattern.tenant_id = " in sql, f"fingerprint 回查未限制租户:\n{sql}"
    assert "solution_pattern.knowledge_base_id = " in sql
    assert all(v == 42 for v in _tenant_params(stmt))


def test_dedupe_patterns_refuses_missing_tenant() -> None:
    import app.knowledge.service as module

    with pytest.raises(TenantScopeError):
        module.KnowledgeService._dedupe_patterns([], tenant_id=None)


# --------------------------------------------------------------------------- #
# 7. KnowledgeService 入口守卫
# --------------------------------------------------------------------------- #
def _bare_knowledge_service() -> Any:
    """不触发 EmbeddingService 初始化,只验证入口守卫。"""
    from app.knowledge.service import KnowledgeService

    return KnowledgeService.__new__(KnowledgeService)


@pytest.mark.parametrize(
    "call",
    [
        lambda svc: svc.search("q", tenant_id=None),
        lambda svc: svc.search_patterns("q", tenant_id=None),
        lambda svc: svc.build_context("q", tenant_id=None),
        lambda svc: svc.generate("q", tenant_id=None),
        lambda svc: svc.rag("q", tenant_id=None),
    ],
    ids=["search", "search_patterns", "build_context", "generate", "rag"],
)
def test_knowledge_service_refuses_missing_tenant(call) -> None:
    with pytest.raises(TenantScopeError):
        call(_bare_knowledge_service())


# --------------------------------------------------------------------------- #
# 8. 落库入口:必须有租户,且不得改写既有归属
# --------------------------------------------------------------------------- #
def test_ingest_result_requires_tenant() -> None:
    from app.knowledge.ingest import ingest_result

    with pytest.raises(TenantScopeError):
        ingest_result(object(), document_id=1, tenant_id=None)


def test_index_document_requires_tenant() -> None:
    from app.knowledge.indexer import KnowledgeIndexer

    with pytest.raises(TenantScopeError):
        KnowledgeIndexer.__new__(KnowledgeIndexer).index_document(
            object(), 1, tenant_id=None, prepare_schema=False
        )


def test_ensure_document_rejects_tenant_rewrite() -> None:
    from app.knowledge.ingest import _ensure_document
    from app.models.document import DocumentRecord

    record = DocumentRecord(
        id=5,
        tenant_id=1,
        file_name="a.docx",
        file_type="docx",
        document_type="historical_bid",
        status="embedded",
    )

    class FakeSession:
        def get(self, model: Any, pk: Any) -> Any:
            return record

    with pytest.raises(TenantScopeError):
        _ensure_document(5, tenant_id=99, session=FakeSession())
    assert record.tenant_id == 1, "被拒绝的写入不得改动既有归属"


def test_ensure_document_adopts_tenant_when_record_has_none() -> None:
    from app.knowledge.ingest import _ensure_document
    from app.models.document import DocumentRecord

    record = DocumentRecord(
        id=6,
        tenant_id=None,
        file_name="a.docx",
        file_type="docx",
        document_type="historical_bid",
        status="embedded",
    )

    class FakeSession:
        def get(self, model: Any, pk: Any) -> Any:
            return record

    _ensure_document(6, tenant_id=7, session=FakeSession())
    assert record.tenant_id == 7


# --------------------------------------------------------------------------- #
# 9. HTTP 层:缺失/冲突租户在进入业务逻辑前就被拒绝
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def http_client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/api/bidox/retrieval/search",
        "/api/bidox/retrieval/patterns",
        "/api/bidox/knowledge/context",
        "/api/bidox/knowledge/generate",
        "/api/bidox/knowledge/rag",
    ],
)
def test_http_endpoints_reject_missing_tenant(http_client, path: str) -> None:
    assert http_client.post(path, json={"query": "q"}).status_code == 422


@pytest.mark.parametrize(
    "path",
    ["/api/bidox/retrieval/search", "/api/bidox/knowledge/rag"],
)
def test_http_endpoints_reject_conflicting_tenant(http_client, path: str) -> None:
    response = http_client.post(path, json={"query": "q", "tenantId": 1, "tenant_id": 2})
    assert response.status_code == 422


def test_http_parse_requires_tenant_only_when_persisting(http_client) -> None:
    # 纯解析不落库:不带租户也通过校验(400 是业务校验,不是 422)
    assert http_client.post("/api/bidox/document/parse", json={"documentId": 1}).status_code != 422
    # 落库:必须带租户
    response = http_client.post(
        "/api/bidox/document/parse", json={"documentId": 1, "persist": True}
    )
    assert response.status_code == 422
