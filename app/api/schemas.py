"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApiResponse(BaseModel):
    """统一响应包装。"""

    code: int = 0
    message: str = "ok"
    data: Any = None


def _normalize_tenant_payload(data: Any) -> Any:
    """把 tenantId / tenant_id 归一化成 tenantId，并在两者同时出现且不一致时直接拒绝。"""
    if not isinstance(data, dict):
        return data
    data = dict(data)
    camel, snake = data.get("tenantId"), data.get("tenant_id")
    if camel is not None and snake is not None and camel != snake:
        raise ValueError("tenantId 与 tenant_id 同时出现且不一致")
    if snake is not None:
        data["tenantId"] = snake
    data.pop("tenant_id", None)
    return data


class _TenantPayload(BaseModel):
    """只做 tenantId / tenant_id 归一化；是否必填由子类决定。"""

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _coerce_tenant(cls, data: Any) -> Any:
        return _normalize_tenant_payload(data)


class TenantScopedRequest(_TenantPayload):
    """要求租户的请求基类：tenantId / tenant_id 均可，但缺失或 null 一律拒绝。

    检索与生成**必须**在明确租户边界内进行；不选知识库只代表不加 knowledge_base_id 条件，
    租户条件永远存在。
    """

    tenant_id: int = Field(alias="tenantId", description="租户 ID(必填)")


class ParseRequest(_TenantPayload):
    """解析请求。兼容 Java camelCase 字段与 Python snake_case 字段。"""

    # Java: parseLogId/documentId/storageKey/storageProvider
    parse_log_id: int | None = Field(default=None, alias="parseLogId")
    file_url: str | None = Field(default=None, alias="fileUrl")
    storage_provider: str | None = Field(default=None, alias="storageProvider")
    storage_key: str | None = Field(default=None, alias="storageKey")

    document_id: int = Field(alias="documentId")
    document_type: str = Field(default="historical_bid", alias="documentType")

    file_name: str | None = Field(default=None, alias="fileName")
    file_type: str | None = Field(default=None, alias="fileType")
    file_size: int | None = Field(default=None, alias="fileSize")

    # True: 解析后向量化落库(pgvector),返回 indexed 统计
    persist: bool = False
    # 索引版本：以 parseLogId 为准；若额外传 indexVersion，两者必须相等（否则拒绝）
    index_version: int | None = Field(default=None, alias="indexVersion")
    # 归属:企业知识库 / 租户 / 企业,冗余到 chunk/pattern 便于过滤
    knowledge_base_id: int | None = Field(default=None, alias="knowledgeBaseId")
    enterprise_id: int | None = Field(default=None, alias="enterpriseId")
    tenant_id: int | None = Field(default=None, alias="tenantId")
    # True: 入库时同步抽取并写入方案组件(solution_pattern);较慢
    with_patterns: bool = Field(default=True, alias="withPatterns")

    @model_validator(mode="after")
    def _require_tenant_when_persist(self) -> "ParseRequest":
        """落库必须有租户边界；仅解析不落库时允许不带。"""
        if self.persist and self.tenant_id is None:
            raise ValueError("persist=True 时必须提供 tenantId/tenant_id")
        if (
            self.parse_log_id is not None
            and self.index_version is not None
            and self.parse_log_id != self.index_version
        ):
            raise ValueError("indexVersion 与 parseLogId 必须相等")
        return self


class LocalParseRequest(_TenantPayload):
    """本地文件解析请求(本地调试用,直接传本地路径)。"""

    path: str
    document_id: int | None = Field(default=None, alias="documentId")
    document_type: str = Field(default="historical_bid", alias="documentType")
    # True: 解析后向量化落库(pgvector);此时 document_id 必填
    persist: bool = False
    index_version: int | None = Field(default=None, alias="indexVersion")
    knowledge_base_id: int | None = Field(default=None, alias="knowledgeBaseId")
    enterprise_id: int | None = Field(default=None, alias="enterpriseId")
    tenant_id: int | None = Field(default=None, alias="tenantId")
    with_patterns: bool = Field(default=True, alias="withPatterns")

    @model_validator(mode="after")
    def _require_tenant_when_persist(self) -> "LocalParseRequest":
        if self.persist and self.tenant_id is None:
            raise ValueError("persist=True 时必须提供 tenantId/tenant_id")
        return self


class DeleteIndexRequest(TenantScopedRequest):
    """删除文档索引请求（幂等 tombstone）。

    `documentId` 必填、`tenantId` 必填；重复调用安全（第二次返回 already_deleted=True）。
    """

    document_id: int = Field(alias="documentId")


class SearchRequest(TenantScopedRequest):
    """检索请求。"""

    query: str
    top_k: int | None = None
    document_type: str | None = None
    document_types: list[str] | None = None
    enterprise_id: int | None = None
    knowledge_base_id: int | None = None


class PatternSearchRequest(TenantScopedRequest):
    """方案组件检索请求。"""

    query: str
    top_k: int | None = None
    enterprise_id: int | None = None
    knowledge_base_id: int | None = None
    pattern_types: list[str] | None = None


class GenerateRequest(TenantScopedRequest):
    """RAG 生成请求(双通道检索 + LLM 生成一步到位)。"""

    query: str
    top_k: int | None = None
    document_type: str | None = None
    enterprise_id: int | None = None
    knowledge_base_id: int | None = None
    # 方案组件召回条数(默认 5)
    pattern_top_k: int | None = None
    # 可选:覆盖默认 RAG system prompt
    system_prompt: str | None = None


class RetrievalConfig(BaseModel):
    """RAG V2 检索配置。"""

    pattern_top_k: int = 5
    chunk_top_k: int = 10
    section_expand: bool = True
    deduplicate: bool = True
    rerank: bool = True


class RagFilters(BaseModel):
    """RAG V2 过滤条件。"""

    pattern_types: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)


class RagRequest(TenantScopedRequest):
    """RAG V2 请求:完整检索链路编排。"""

    query: str
    enterprise_id: int | None = None
    knowledge_base_id: int | None = None
    document_types: list[str] = Field(default_factory=list)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    filters: RagFilters = Field(default_factory=RagFilters)
    system_prompt: str | None = None
    # 当前招标约束(可选):requirements/score_items 来自 /tender/analyze;或传 tender_text 现场提取
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    score_items: list[dict[str, Any]] = Field(default_factory=list)
    tender_text: str | None = None


class QaHistoryMessage(BaseModel):
    """企业知识问答最近历史消息。Java 只传最近 6 个已完成轮次。"""

    role: str
    content: str


class QaStreamRequest(TenantScopedRequest):
    """企业知识问答流式请求（Java → Python 内部接口）。"""

    question: str = Field(min_length=1, max_length=8000)
    knowledge_base_id: int | None = Field(default=None, alias="knowledgeBaseId")
    history: list[QaHistoryMessage] = Field(default_factory=list, max_length=12)
    # Java 从持久化 expires_at 计算剩余预算；Python 不能重新起算完整 180 秒。
    timeout_millis: int = Field(default=180_000, alias="timeoutMillis", ge=1, le=180_000)
    top_k: int = Field(default=10, alias="topK", ge=1, le=50)
    rerank: bool = True


class AnalyzeRequest(BaseModel):
    """招标分析请求。"""

    text: str
    document_id: int | None = None


class TenderAnalyzeLocalRequest(BaseModel):
    """本地招标文件分析请求(直接传路径,内部解析→提取)。"""

    path: str
    document_type: str = "tender"
