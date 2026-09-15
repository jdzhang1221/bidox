"""MQ 任务消息模型(与 Java 侧约定)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ParseTaskMessage(BaseModel):
    """文档解析任务。"""

    task_id: str
    document_id: int
    file_url: str  # minio://bidox/xxx.docx 或本地相对路径 bid-document/...
    document_type: str = "historical_bid"  # tender / historical_bid / ...
    storage_provider: str | None = None  # local / minio;为空时按全局配置
    # 解析任务最终要落库,必须带租户边界(见 app/core/tenant.py)
    tenant_id: int
    enterprise_id: int | None = None
    project_id: int | None = None
    # 索引版本(来自 Java parseLogId);缺省时由 ingest 按 index_version=1 处理
    index_version: int | None = None


class EmbeddingTaskMessage(BaseModel):
    """向量化任务。"""

    task_id: str
    document_id: int
    # 向量化即落库,必须带租户边界(见 app/core/tenant.py)
    tenant_id: int
    knowledge_base_id: int | None = None
    enterprise_id: int | None = None
    # 索引版本:守卫据此拒绝旧版本任务晚到写入
    index_version: int | None = None
    chunk_ids: list[int] = Field(default_factory=list)


class AnalysisTaskMessage(BaseModel):
    """招标结构化分析任务。"""

    task_id: str
    document_id: int
    document_type: str = "tender"
