"""语义切分:把超长文本按语义边界(段落/句子/标题)切成不超过 max_chars 的片段。"""

from __future__ import annotations

import re

from app.core.config import settings

# 句子/段落边界
_SENT_END = re.compile(r"(?<=[。！？!?；;])\s*")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def semantic_split(
    text: str,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """语义切分。

    优先按空行(段落)切,超长段落再按句子切;保留 overlap 提升上下文连续性。
    """
    max_chars = max_chars or settings.chunk_max_chars
    overlap = overlap if overlap is not None else settings.chunk_overlap

    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            # 超长段落 -> 先落当前,再按句子切该段落
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long(para, max_chars, overlap))
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para

    if current:
        chunks.append(current)
    return _apply_overlap(chunks, overlap)


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长段落按句子切。"""
    sentences = [s for s in _SENT_END.split(text) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        candidate = f"{current}{sent}" if current else sent
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # 单句仍超长 -> 硬切
            if len(sent) > max_chars:
                current = ""
                for i in range(0, len(sent), max_chars - overlap):
                    chunks.append(sent[i : i + max_chars])
            else:
                current = sent
    if current:
        chunks.append(current)
    return _apply_overlap(chunks, overlap)


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """相邻 chunk 之间加 overlap(简单前文回填)。"""
    if overlap <= 0 or len(chunks) <= 1:
        return chunks
    result = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            tail = chunks[i - 1][-overlap:]
            chunk = tail + chunk
        result.append(chunk)
    return result
