"""FastAPI 应用入口。

启动:
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api.router import api_router
from app.core.config import settings
from app.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="BidOx AI 标书生成助手 - 文档解析 / 知识库 / RAG / 招标分析",
)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", tags=["health"])
def root() -> dict:
    return {"app": settings.app_name, "version": __version__, "status": "ok"}


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.debug)
