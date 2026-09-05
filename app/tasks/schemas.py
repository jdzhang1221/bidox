"""MQ 任务消息模型(与 Java 侧约定)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ParseTaskMessage(BaseModel):
    """文档解析任务。"""

    task_id: str
    document_id: int
    file_url: str  # minio://bidox/xxx.docx
    document_type: str = "historical_bid"  # tender / historical_bid / ...
    tenant_id: int | None = None
    enterprise_id: int | None = None
    project_id: int | None = None


class EmbeddingTaskMessage(BaseModel):
    """向量化任务。"""

    task_id: str
    document_id: int
    chunk_ids: list[int] = Field(default_factory=list)


class AnalysisTaskMessage(BaseModel):
    """招标结构化分析任务。"""

    task_id: str
    document_id: int
    document_type: str = "tender"
