"""有界同步工作池：隔离 SQLAlchemy/Embedding/Reranker，避免阻塞 asyncio event loop。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import BoundedSemaphore
from typing import Callable, TypeVar

T = TypeVar("T")


class ExecutorBusyError(RuntimeError):
    """有界工作池已满。"""


class BoundedExecutor:
    def __init__(self, max_workers: int, queue_capacity: int, thread_name_prefix: str) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._slots = BoundedSemaphore(max_workers + queue_capacity)

    async def run(self, func: Callable[..., T], /, *args, **kwargs) -> T:
        """提交同步函数；容量已满立即拒绝，不制造无界排队。"""
        if not self._slots.acquire(blocking=False):
            raise ExecutorBusyError("QA 检索工作池已满")
        try:
            future = self._executor.submit(partial(func, *args, **kwargs))
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        return await asyncio.wrap_future(future)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


qa_retrieval_executor = BoundedExecutor(max_workers=4, queue_capacity=8, thread_name_prefix="qa-retrieval")
