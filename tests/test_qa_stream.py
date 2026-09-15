"""企业知识问答异步流与来源分数语义测试。"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.api.schemas import QaStreamRequest
from app.core.config import settings
from app.knowledge.qa_prompt import build_retrieval_query
from app.knowledge.qa_stream import PreparedQa, _prepare_sync, stream_qa
from app.llm.base import LLMStreamChunk
from app.llm.openai_compat import OpenAICompatClient
from app.retrieval import reranker as reranker_module
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.reranker import Reranker


def _events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    out = []
    for raw in chunks:
        text = raw.decode("utf-8")
        lines = text.strip().splitlines()
        out.append((lines[0].split(":", 1)[1].strip(), json.loads(lines[1][5:].strip())))
    return out


def test_follow_up_retrieval_query_uses_previous_user_question() -> None:
    history = [
        {"role": "user", "content": "质量认证证书的有效期是什么？"},
        {"role": "assistant", "content": "三年。"},
    ]
    assert build_retrieval_query("该证书呢", history) == "质量认证证书的有效期是什么？\n追问：该证书呢"
    assert build_retrieval_query("重新介绍企业资质", history) == "重新介绍企业资质"


def test_schema_requires_tenant_and_accepts_camel_case() -> None:
    request = QaStreamRequest.model_validate(
        {"tenantId": 9, "question": "问题", "knowledgeBaseId": 3, "timeoutMillis": 1000}
    )
    assert request.tenant_id == 9
    assert request.knowledge_base_id == 3
    with pytest.raises(Exception):
        QaStreamRequest.model_validate({"question": "缺租户"})


def test_rrf_and_reranker_preserve_original_similarity(monkeypatch: pytest.MonkeyPatch) -> None:
    vector = [{"id": 1, "similarity": 0.87, "retrieval_score": 0.87, "score": 0.87}]
    keyword = [{"id": 1, "similarity": None, "retrieval_score": 1.0, "score": 1.0}]
    fused = HybridRetriever._rrf(vector, keyword)
    assert fused[0]["similarity"] == 0.87
    assert fused[0]["score_type"] == "rrf"
    assert fused[0]["retrieval_score"] != 0.87

    reranker = Reranker()
    # 模型是**进程级缓存**（见 app/retrieval/reranker.py）：往缓存里注入假模型，
    # 既不加载真实的 2GB 模型，也不依赖实例属性。monkeypatch.setitem 会自动还原。
    monkeypatch.setitem(
        reranker_module._MODEL_CACHE,
        settings.reranker_model,
        SimpleNamespace(compute_score=lambda pairs, normalize=True: [0.94]),
    )
    reranked = reranker.rerank("q", fused, top_k=1)
    assert reranked[0]["similarity"] == 0.87
    assert reranked[0]["retrieval_score"] == 0.94
    assert reranked[0]["score_type"] == "reranker"


def test_prepare_qa_keeps_enterprise_facts_unredacted(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = {
        "id": 301,
        "section_id": 101,
        "document_id": 88,
        "page_start": 12,
        "page_end": 12,
        "index_version": 901,
        "similarity": 0.8731,
        "retrieval_score": 0.9427,
        "score_type": "reranker",
        "content": "某科技公司证书有效期至2027年3月，注册资金500万元。",
    }

    monkeypatch.setattr(
        "app.knowledge.qa_stream.KnowledgeService.search",
        lambda self, *args, **kwargs: [dict(evidence)],
    )
    prepared = _prepare_sync("有效期呢", [], 9, None, 10, True)
    prompt = prepared.messages[-1]["content"]
    assert "2027年3月" in prompt
    assert "500万元" in prompt
    assert prepared.sources[0] == {
        "chunkId": 301,
        "sectionId": 101,
        "documentId": 88,
        "documentName": None,
        "pageStart": 12,
        "pageEnd": 12,
        "similarity": 0.8731,
        "retrievalScore": 0.9427,
        "scoreType": "reranker",
        "snippet": evidence["content"],
        "indexVersion": 901,
    }


@pytest.mark.asyncio
async def test_openai_stream_parses_delta_finish_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original(transport=transport, timeout=kwargs.get("timeout")),
    )
    client = OpenAICompatClient(base_url="http://test", api_key="k", model="m")
    chunks = [chunk async for chunk in client.stream_chat([{"role": "user", "content": "hi"}])]
    assert [chunk.text for chunk in chunks] == ["你", "好"]
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_qa_emits_meta_delta_done(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGateway:
        async def stream_chat(self, messages, timeout=None):
            yield LLMStreamChunk(text="第一段")
            yield LLMStreamChunk(text="第二段", finish_reason="stop")

    monkeypatch.setattr("app.knowledge.qa_stream.get_llm", lambda: FakeGateway())
    prepared = PreparedQa(
        retrieval_query="检索词",
        messages=[{"role": "user", "content": "问题"}],
        sources=[{"chunkId": 1}],
    )
    chunks = [chunk async for chunk in stream_qa(prepared, deadline=time.monotonic() + 2)]
    events = _events(chunks)
    assert [name for name, _ in events] == ["meta", "delta", "delta", "done"]
    assert events[0][1]["sources"] == [{"chunkId": 1}]
    assert events[-1][1]["finishReason"] == "stop"


@pytest.mark.asyncio
async def test_stream_qa_maps_timeout_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowGateway:
        async def stream_chat(self, messages, timeout=None):
            await asyncio.sleep(1)
            yield LLMStreamChunk(text="never")

    monkeypatch.setattr("app.knowledge.qa_stream.get_llm", lambda: SlowGateway())
    prepared = PreparedQa("q", [{"role": "user", "content": "q"}], [])
    chunks = [chunk async for chunk in stream_qa(prepared, deadline=time.monotonic() + 0.02)]
    events = _events(chunks)
    assert [name for name, _ in events] == ["meta", "error"]
    assert events[-1][1]["code"] == "UPSTREAM_TIMEOUT"
