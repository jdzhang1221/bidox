"""数据库引擎与会话(PostgreSQL + pgvector)。

采用懒初始化:仅当实际需要数据库时才创建引擎,避免骨架在无 DB 环境 import 失败。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    """ORM 基类。"""


def get_engine() -> Engine:
    """懒加载创建数据库引擎。"""
    global _engine, _session_factory
    if _engine is None:
        logger.info("创建数据库引擎: %s", _mask_url(settings.database_url))
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_session() -> Session:
    """获取一个数据库会话。"""
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    """上下文管理器形式的会话,自动提交/回滚。"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """建表(开发环境用,生产建议用迁移工具)。"""
    from app.models import (  # noqa: F401  # 触发模型注册
        DocumentChunk,
        DocumentIndexGuard,
        DocumentPatternJob,
        DocumentRecord,
        DocumentSection,
        KnowledgeBase,
        PatternSource,
        SolutionPattern,
    )

    engine = get_engine()
    Base.metadata.create_all(engine)
    logger.info("数据库表结构已同步")


def _mask_url(url: str) -> str:
    """隐藏连接串中的密码,便于日志输出。"""
    try:
        return url.split("@")[-1].split("/")[0]
    except Exception:  # pragma: no cover - 防御性
        return "<db>"


def add_vector_extension() -> None:
    """创建 pgvector 扩展(幂等)。"""
    from sqlalchemy import text

    with session_scope() as session:
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    logger.info("pgvector 扩展已就绪")


__all__ = [
    "Base",
    "add_vector_extension",
    "get_engine",
    "get_session",
    "init_db",
    "session_scope",
    "Session",
    "Any",
]
