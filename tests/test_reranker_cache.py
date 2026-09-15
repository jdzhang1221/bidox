"""Reranker 模型缓存测试。

背景（真实事故）：`KnowledgeService()` 是**每请求新建**的（`app/api/knowledge.py` 与
`app/knowledge/qa_stream.py` 都直接 `KnowledgeService()`），它内部又新建
`HybridRetriever()` → `Reranker()`。模型原先挂在实例上，导致每个问答请求都重新加载一次
reranker 模型（本机实测冷启动 1~2 分钟，离线时还要先等若干次 huggingface HEAD 超时），
请求会在「还没开始检索」的阶段就把预算耗尽。

本文件锁住两条契约：① 模型在进程内只加载一次；② 加载失败也被缓存（否则每个请求都要重付超时代价）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.retrieval import reranker as reranker_module
from app.retrieval.reranker import Reranker


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """模块级缓存是进程级状态，测试之间必须隔离，否则用例会互相污染。"""
    reranker_module._MODEL_CACHE.clear()
    yield
    reranker_module._MODEL_CACHE.clear()


def _counting_loader(sentinel: Any, calls: list[int]):
    def loader(self: Reranker) -> Any:
        calls.append(1)
        return sentinel

    return loader


def test_model_is_loaded_once_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    sentinel = object()
    monkeypatch.setattr(Reranker, "_load_model", _counting_loader(sentinel, calls))

    first = Reranker()
    second = Reranker()

    assert first._get_model() is sentinel
    assert second._get_model() is sentinel
    assert len(calls) == 1, "第二个实例必须复用进程级缓存，而不是重新加载模型"


def test_failed_load_is_cached_and_marks_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(Reranker, "_load_model", _counting_loader(None, calls))

    first = Reranker()
    second = Reranker()

    assert first._get_model() is None
    assert first._fallback is True
    assert second._get_model() is None
    assert second._fallback is True
    assert len(calls) == 1, "加载失败也必须缓存，否则每个请求都会重付一次超时"


def test_rerank_falls_back_to_head_of_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Reranker, "_load_model", _counting_loader(None, []))

    candidates = [{"content": f"c{i}"} for i in range(5)]

    result = Reranker().rerank("q", candidates, top_k=2)

    assert result == candidates[:2]


def test_cache_key_follows_configured_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """换模型名必须重新加载：缓存键是模型名，不能把旧模型当成新模型的实例。"""
    calls: list[int] = []
    monkeypatch.setattr(Reranker, "_load_model", _counting_loader(object(), calls))

    Reranker()._get_model()
    monkeypatch.setattr(reranker_module.settings, "reranker_model", "another/model")
    Reranker()._get_model()

    assert len(calls) == 2
