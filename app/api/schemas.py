"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    """统一响应包装。"""

    code: int = 0
    message: str = "ok"
    data: Any = None


class ParseRequest(BaseModel):
    """解析请求。"""

    file_url: str
    document_id: int
    document_type: str = "historical_bid"
    # True: 解析后向量化落库(pgvector),返回 indexed 统计
    persist: bool = False
    # 归属(可选):企业知识库 / 租户 / 企业,冗余到 chunk/pattern 便于过滤
    knowledge_base_id: int | None = None
    enterprise_id: int | None = None
    tenant_id: int | None = None
    # True: 入库时同步抽取并写入方案组件(solution_pattern);较慢
    with_patterns: bool = True


class LocalParseRequest(BaseModel):
    """本地文件解析请求(本地调试用,直接传本地路径)。"""

    path: str
    document_id: int | None = None
    document_type: str = "historical_bid"
    # True: 解析后向量化落库(pgvector);此时 document_id 必填
    persist: bool = False
    knowledge_base_id: int | None = None
    enterprise_id: int | None = None
    tenant_id: int | None = None
    with_patterns: bool = True


class SearchRequest(BaseModel):
    """检索请求。"""

    query: str
    top_k: int | None = None
    document_type: str | None = None
    document_types: list[str] | None = None
    enterprise_id: int | None = None
    knowledge_base_id: int | None = None


class PatternSearchRequest(BaseModel):
    """方案组件检索请求。"""

    query: str
    top_k: int | None = None
    enterprise_id: int | None = None
    knowledge_base_id: int | None = None
    pattern_types: list[str] | None = None


class GenerateRequest(BaseModel):
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


class RagRequest(BaseModel):
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


class AnalyzeRequest(BaseModel):
    """招标分析请求。"""

    text: str
    document_id: int | None = None


class TenderAnalyzeLocalRequest(BaseModel):
    """本地招标文件分析请求(直接传路径,内部解析→提取)。"""

    path: str
    document_type: str = "tender"
