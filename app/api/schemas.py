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


class SearchRequest(BaseModel):
    """检索请求。"""

    query: str
    top_k: int | None = None
    document_type: str | None = None
    enterprise_id: int | None = None


class AnalyzeRequest(BaseModel):
    """招标分析请求。"""

    text: str
    document_id: int | None = None
