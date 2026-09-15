"""企业知识问答：同步检索准备 + 真异步 LLM 流。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator

from app.core.doc_types import resolve_qa_document_types
from app.core.executors import qa_retrieval_executor
from app.knowledge.qa_prompt import build_messages, build_retrieval_query
from app.knowledge.service import KnowledgeService
from app.llm.base import LLMError
from app.llm.gateway import get_llm

_QUEUE_SIZE = 16
_DELTA_CHARS = 16_000
_SNIPPET_CHARS = 500
_CANCEL_WAIT_SECONDS = 5.0


@dataclass(slots=True)
class PreparedQa:
    retrieval_query: str
    messages: list[dict[str, str]]
    sources: list[dict[str, Any]]


def _source_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """把内部检索字典收敛成稳定来源契约；不暴露 embedding / _rerank_text。"""
    return {
        "chunkId": item.get("id"),
        "sectionId": item.get("section_id"),
        "documentId": item.get("document_id"),
        "documentName": None,  # Java 按业务库 display_name 批量回填并持久化为快照
        "pageStart": item.get("page_start"),
        "pageEnd": item.get("page_end"),
        "similarity": item.get("similarity"),
        "retrievalScore": item.get("retrieval_score", item.get("score")),
        "scoreType": item.get("score_type"),
        "snippet": str(item.get("content") or "")[:_SNIPPET_CHARS],
        "indexVersion": item.get("index_version"),
    }


def _prepare_sync(
    question: str,
    history: list[dict[str, Any]],
    tenant_id: int,
    knowledge_base_id: int | None,
    top_k: int,
    rerank: bool,
) -> PreparedQa:
    retrieval_query = build_retrieval_query(question, history)
    service = KnowledgeService()
    evidences = service.search(
        retrieval_query,
        tenant_id=tenant_id,
        top_k=top_k,
        document_types=resolve_qa_document_types(None),
        knowledge_base_id=knowledge_base_id,
        rerank=rerank,
    )
    # QA 证据是当前企业知识，redact_facts=False：数字/日期/资质必须原样保留。
    evidence_context = service._format_context(evidences, prefix="来源", redact_facts=False)
    if not evidence_context:
        evidence_context = "(未检索到可用企业知识证据)"
    return PreparedQa(
        retrieval_query=retrieval_query,
        messages=build_messages(question, history, evidence_context),
        sources=[_source_snapshot(item) for item in evidences],
    )


async def prepare_qa(
    *,
    question: str,
    history: list[dict[str, Any]],
    tenant_id: int,
    knowledge_base_id: int | None,
    top_k: int,
    rerank: bool,
    deadline: float,
) -> PreparedQa:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("QA 检索准备已超过绝对 deadline")
    async with asyncio.timeout(remaining):
        return await qa_retrieval_executor.run(
            _prepare_sync,
            question,
            history,
            tenant_id,
            knowledge_base_id,
            top_k,
            rerank,
        )


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


async def stream_qa(prepared: PreparedQa, *, deadline: float) -> AsyncIterator[bytes]:
    """产生 meta → delta* → done/error；producer/consumer 之间是有界队列。"""
    yield _sse(
        "meta",
        {"retrievalQuery": prepared.retrieval_query, "sources": prepared.sources},
    )
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=_QUEUE_SIZE)

    async def producer() -> None:
        finish_reason = "stop"
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QA 流已超过绝对 deadline")
            async with asyncio.timeout(remaining):
                async for chunk in get_llm().stream_chat(prepared.messages, timeout=remaining):
                    if chunk.finish_reason:
                        finish_reason = chunk.finish_reason
                    text = chunk.text
                    # 单 SSE 事件必须小于 64KiB；按字符切成更保守的 16K 块。
                    for start in range(0, len(text), _DELTA_CHARS):
                        await queue.put(("delta", {"text": text[start : start + _DELTA_CHARS]}))
            await queue.put(("done", {"finishReason": finish_reason}))
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await queue.put(("error", {"code": "UPSTREAM_TIMEOUT", "message": "回答生成超时"}))
        except LLMError:
            # 不把上游错误正文透传给下游或日志。
            await queue.put(("error", {"code": "UPSTREAM_FAILED", "message": "调用 AI 问答服务失败"}))
        except Exception:
            await queue.put(("error", {"code": "UPSTREAM_FAILED", "message": "问答生成失败"}))

    task = asyncio.create_task(producer(), name="qa-stream-producer")
    try:
        while True:
            event, payload = await queue.get()
            yield _sse(event, payload)
            if event in {"done", "error"}:
                break
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=_CANCEL_WAIT_SECONDS)
