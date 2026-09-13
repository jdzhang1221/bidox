"""知识库查询服务:对外暴露统一检索入口(四层 Evidence Pack + Reranker 重排 + 事实隔离)。"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.pattern import PatternSource, SolutionPattern
from app.retrieval.hybrid import DEFAULT_PATTERN_TOP_K, HybridRetriever
from app.retrieval.query import QueryUnderstanding
from app.retrieval.section import SectionRetriever

logger = get_logger(__name__)

_DEFAULT_RAG_SYSTEM = (
    "你是标书撰写助手。请严格基于提供的【参考资料】回答用户问题。\n"
    "参考资料分四层:【当前招标要求】是本次项目的约束,【方案组件】是已去事实化的可复用方案模板,"
    "【章节体系】是命中章节及其父/兄弟章节的关联结构,【历史证据】是过去标书的具体写法,"
    "【企业事实】是本次投标企业的真实资质/人员/案例。\n"
    "硬性要求:\n"
    "1. 只使用参考资料中的信息,不要编造事实或数据。\n"
    "2. 回答时用 [依据N] 标注引用来源(N 对应历史证据编号)。\n"
    "3. 优先参考【方案组件】组织框架,再用【历史证据】填充写法;以【当前招标要求】为准绳。\n"
    "4. 严禁复制历史标书里的具体数字、单位、人名、机构名、项目名、日期;历史证据里残留的这些事实"
    "一律不得写进新标书,只能借鉴其方法与结构(注意:不得提及任何具体机构名或历史项目名)。\n"
    "5. 若参考资料不足以回答问题,请明确说明缺口,不要强行拼凑。\n"
    "6. 保持专业、结构化的标书语言风格。"
)

# 方案正文拼进 prompt 时的截断上限(避免单章节全文过长撑爆上下文)
_PATTERN_CONTENT_MAX_CHARS = 3000
# 章节体系里单章节正文预览上限
_SECTION_CONTENT_MAX_CHARS = 600

# --- 历史事实脱敏:证据层(历史标书)注入前抹掉具体数字/机构/日期 ---
# 历史证据的用途是借鉴「写法与结构」,其中残留的具体事实一律不得进入新标书;
# 与其依赖 LLM 遵守"严禁复制"的软约束,不如在注入前确定性地抹掉事实本身。
_NUM_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?(?=[家个名人项套组台次份处年日月天周元万亿千百%‰倍米公里吨公斤克秒分时])"
)
_ORG_RE = re.compile(
    r"[一-龥]{2,12}(?:公司|事务所|集团|中心|局|院|协会|委员会|银行|学校|医院|工会|大队|支队)"
)
_DATE_RE = re.compile(r"\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?")


def _date_repl(m: re.Match) -> str:
    s = m.group(0)
    if "日" in s:
        return "某年某月某日"
    if "月" in s:
        return "某年某月"
    return "某年"


def _redact_facts(text: str) -> str:
    """把历史事实(数字+单位、机构名、日期)替换为通用占位,只保留写法与结构。"""
    t = _DATE_RE.sub(_date_repl, text)
    t = _NUM_UNIT_RE.sub("某", t)
    t = _ORG_RE.sub("某机构", t)
    return t


class KnowledgeService:
    """知识库统一查询。"""

    def __init__(self) -> None:
        self._retriever = HybridRetriever()

    # --- 证据通道(chunk) ---
    def search(
        self,
        query: str,
        top_k: int | None = None,
        document_types: list[str] | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        """混合检索,返回带可追溯信息的 chunk 列表。"""
        return self._retriever.search(
            query,
            top_k=top_k,
            document_types=document_types,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            rerank=rerank,
        )

    # --- 方案通道(pattern) ---
    def search_patterns(
        self,
        query: str,
        top_k: int | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        pattern_types: list[str] | None = None,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        """方案组件检索,并回填 source 溯源(document/section/page)。"""
        patterns = self._retriever.search_patterns(
            query,
            top_k=top_k,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            pattern_types=pattern_types,
            rerank=rerank,
        )
        return self._enrich_pattern_sources(patterns)

    # --- 溯源回填 ---
    @staticmethod
    def _enrich_pattern_sources(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = [p.get("pattern_id") for p in patterns if p.get("pattern_id") is not None]
        if not ids:
            return patterns
        with session_scope() as session:
            rows = (
                session.execute(
                    select(PatternSource).where(PatternSource.pattern_id.in_(ids))
                )
                .scalars()
                .all()
            )
        by_pid: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by_pid.setdefault(r.pattern_id, []).append(
                {
                    "document_id": r.document_id,
                    "section_id": r.section_id,
                    "chunk_id": r.chunk_id,
                    "source_type": r.source_type,
                    "page_start": r.page_start,
                    "page_end": r.page_end,
                }
            )
        for p in patterns:
            p["sources"] = by_pid.get(p.get("pattern_id"), [])
        return patterns

    # --- fingerprint 去重 + 跨文档聚合 ---
    @staticmethod
    def _dedupe_patterns(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按 fingerprint 去重:同指纹只保留一个代表(score 最高,即 patterns 首个出现),聚合全部溯源。"""
        if not patterns:
            return []
        fps = [p.get("fingerprint") for p in patterns if p.get("fingerprint")]
        if not fps:
            return patterns

        with session_scope() as session:
            rows = (
                session.execute(
                    select(SolutionPattern).where(SolutionPattern.fingerprint.in_(fps))
                )
                .scalars()
                .all()
            )
        by_fp: dict[str, list[SolutionPattern]] = {}
        for r in rows:
            by_fp.setdefault(r.fingerprint, []).append(r)

        all_ids = [r.id for r in rows]
        src_map: dict[int, list[dict[str, Any]]] = {}
        if all_ids:
            with session_scope() as session:
                srcs = (
                    session.execute(
                        select(PatternSource).where(PatternSource.pattern_id.in_(all_ids))
                    )
                    .scalars()
                    .all()
                )
            for s in srcs:
                src_map.setdefault(s.pattern_id, []).append(
                    {
                        "document_id": s.document_id,
                        "section_id": s.section_id,
                        "chunk_id": s.chunk_id,
                        "source_type": s.source_type,
                        "page_start": s.page_start,
                        "page_end": s.page_end,
                    }
                )

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in patterns:
            fp = p.get("fingerprint")
            if not fp:
                deduped.append(p)
                continue
            if fp in seen:
                continue
            seen.add(fp)
            rep = dict(p)
            group = by_fp.get(fp, [])
            # 聚合该指纹下所有 pattern 的溯源,按 (document_id, section_id) 去重
            sources: list[dict[str, Any]] = []
            seen_src: set[tuple] = set()
            for r in group:
                for src in src_map.get(r.id, []):
                    key = (src.get("document_id"), src.get("section_id"))
                    if key not in seen_src:
                        seen_src.add(key)
                        sources.append(src)
            rep["sources"] = sources or p.get("sources", [])
            rep["duplicate_count"] = len(group)
            deduped.append(rep)
        return deduped

    # --- Pattern 知识节点:source 扩到兄弟章节 ---
    @staticmethod
    def _expand_pattern_sources(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把每个 pattern 的 source 扩到「命中→父→兄弟」章节,形成方案知识节点。"""
        retriever = SectionRetriever()
        for p in patterns:
            sources = p.get("sources", [])
            sec_ids = {s.get("section_id") for s in sources if s.get("section_id")}
            if not sec_ids:
                continue
            related = retriever.expand(list(sec_ids))
            seen = set(sec_ids)
            for r in related:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                sources.append(
                    {
                        "document_id": r.get("document_id"),
                        "section_id": r.get("id"),
                        "chunk_id": None,
                        "source_type": "related",
                        "section_no": r.get("section_no"),
                        "title": r.get("title"),
                        "page_start": r.get("page_start"),
                        "page_end": r.get("page_end"),
                    }
                )
            p["sources"] = sources
        return patterns

    # --- Evidence Pack 组装 ---
    @staticmethod
    def _format_context(
        results: list[dict[str, Any]], prefix: str = "依据", redact_facts: bool = False
    ) -> str:
        """把检索结果拼成带溯源标注的 context。

        redact_facts=True 时对历史证据正文做事实脱敏(数字/机构/日期→占位),
        仅用于「历史证据」层;企业事实/当前招标要求等当前事实不脱敏。
        """
        parts = []
        for i, r in enumerate(results, 1):
            source = f"文档#{r.get('document_id')}"
            if r.get("page_start"):
                source += f" 第{r['page_start']}页"
            if r.get("section_type"):
                source += f" [{r['section_type']}]"
            content = r.get("content", "")
            if redact_facts:
                content = _redact_facts(content)
            parts.append(f"[{prefix}{i}] {source}\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _format_patterns(patterns: list[dict[str, Any]]) -> str:
        parts = []
        for i, p in enumerate(patterns, 1):
            lines = [f"[方案{i}] {p.get('code')} {p.get('name')} (type={p.get('type')})"]
            if p.get("duplicate_count") and p["duplicate_count"] > 1:
                lines.append(f"复用次数: {p['duplicate_count']}")
            if p.get("scenario"):
                lines.append(f"场景: {p['scenario']}")
            if p.get("summary"):
                lines.append(f"摘要: {p['summary']}")
            if p.get("structure"):
                lines.append("结构: " + " / ".join(p["structure"]))
            # 只注入去事实化正文,严禁注入原始 content(避免历史事实污染)
            body = p.get("generalized_content") or ""
            if body:
                if len(body) > _PATTERN_CONTENT_MAX_CHARS:
                    body = body[:_PATTERN_CONTENT_MAX_CHARS] + "\n…(正文过长已截断)"
                lines.append(f"正文(已去事实化):\n{body}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def _format_sections(sections: list[dict[str, Any]]) -> str:
        if not sections:
            return "(无章节体系)"
        parts = []
        for s in sections:
            no = s.get("section_no") or ""
            title = s.get("title") or ""
            indent = "  " * max(0, int(s.get("level") or 1) - 1)
            head = f"{indent}{no} {title}".strip()
            if s.get("section_type"):
                head += f" [{s['section_type']}]"
            lines = [head]
            content = (s.get("content") or "").strip()
            if content:
                content = _redact_facts(content)
                if len(content) > _SECTION_CONTENT_MAX_CHARS:
                    content = content[:_SECTION_CONTENT_MAX_CHARS] + "…"
                lines.append(f"{indent}  预览: {content}")
            parts.append("\n".join(lines))
        return "\n".join(parts)

    @staticmethod
    def _format_requirements(
        requirements: list[dict[str, Any]], score_items: list[dict[str, Any]]
    ) -> str:
        parts: list[str] = []
        if requirements:
            parts.append("招标要求:")
            for i, r in enumerate(requirements, 1):
                cat = f"[{r.get('category', '')}] " if r.get("category") else ""
                parts.append(f"  R{i} {cat}{r.get('description', '')}")
        if score_items:
            parts.append("评分点:")
            for i, s in enumerate(score_items, 1):
                parts.append(f"  S{i} {s.get('item', '')} ({s.get('score', '')}分): {s.get('criteria', '')}")
        return "\n".join(parts) if parts else ""

    def build_evidence_pack(
        self,
        query: str,
        patterns: list[dict[str, Any]],
        evidences: list[dict[str, Any]],
        sections: list[dict[str, Any]] | None = None,
        enterprise_facts: list[dict[str, Any]] | None = None,
        current_requirements: list[dict[str, Any]] | None = None,
        score_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """组装四层 Evidence Pack(当前要求 / 方案组件 / 历史证据 / 企业事实)。"""
        return {
            "query": query,
            "current_requirements": current_requirements or [],
            "score_items": score_items or [],
            "patterns": patterns,
            "sections": sections or [],
            "evidences": evidences,
            "enterprise_facts": enterprise_facts or [],
        }

    @staticmethod
    def _format_pack(pack: dict[str, Any]) -> str:
        req_text = KnowledgeService._format_requirements(
            pack.get("current_requirements", []), pack.get("score_items", [])
        )
        patterns_text = (
            KnowledgeService._format_patterns(pack["patterns"]) if pack["patterns"] else "(无方案组件召回)"
        )
        sections_text = KnowledgeService._format_sections(pack.get("sections", []))
        evidences_text = (
            KnowledgeService._format_context(pack["evidences"], redact_facts=True)
            if pack["evidences"]
            else "(无历史证据召回)"
        )
        facts_text = (
            KnowledgeService._format_context(pack.get("enterprise_facts", []), prefix="企业")
            if pack.get("enterprise_facts")
            else "(无企业事实)"
        )
        blocks = []
        if req_text:
            blocks.append(f"【当前招标要求】\n{req_text}")
        blocks.append(f"【方案组件】(已去事实化的可复用方案模板)\n{patterns_text}")
        blocks.append(f"【章节体系】(命中章节及其父/兄弟章节)\n{sections_text}")
        blocks.append(f"【历史证据】(过去标书的具体写法,仅参考写法,勿复制具体事实)\n{evidences_text}")
        blocks.append(f"【企业事实】\n{facts_text}")
        return "\n\n".join(blocks)

    def build_context(self, query: str, top_k: int | None = None) -> str:
        """把检索结果拼成 evidence context(供 LLM 使用)。"""
        results = self.search(query, top_k=top_k)
        return self._format_context(results)

    def generate(
        self,
        query: str,
        top_k: int | None = None,
        document_type: str | None = None,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        system_prompt: str | None = None,
        pattern_top_k: int | None = None,
    ) -> dict[str, Any]:
        """双通道 RAG 一步到位(轻量别名,复用 rag 编排)。

        返回 {query, answer, patterns, sources, context},其中:
        - patterns: 方案级召回(含 sources 溯源)
        - sources: 证据级召回(chunk,含 rank/score/page)
        - context: 拼好的 Evidence Pack prompt
        """
        result = self.rag(
            query,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            document_types=[document_type] if document_type else None,
            pattern_top_k=pattern_top_k,
            chunk_top_k=top_k,
        )
        sources = [
            {
                "rank": i + 1,
                "title": r.get("title"),
                "section_type": r.get("section_type"),
                "document_id": r.get("document_id"),
                "page_start": r.get("page_start"),
                "score": round(float(r.get("score", 0.0)), 4),
                "content_preview": (r.get("content") or "")[:200],
            }
            for i, r in enumerate(result["evidences"])
        ]
        return {
            "query": query,
            "answer": result["answer"],
            "patterns": result["patterns"],
            "sources": sources,
            "context": result["context"],
        }

    def rag(
        self,
        query: str,
        *,
        enterprise_id: int | None = None,
        knowledge_base_id: int | None = None,
        document_types: list[str] | None = None,
        pattern_top_k: int | None = None,
        chunk_top_k: int | None = None,
        pattern_types: list[str] | None = None,
        section_expand: bool = True,
        deduplicate: bool = True,
        rerank: bool = True,
        system_prompt: str | None = None,
        requirements: list[dict[str, Any]] | None = None,
        score_items: list[dict[str, Any]] | None = None,
        tender_text: str | None = None,
    ) -> dict[str, Any]:
        """RAG V2 全链路编排:
        intent → (pattern type 过滤 + chunk) → 去重 → pattern 知识节点 → section expansion
        → 企业事实 → 四层 Evidence Pack → LLM。

        返回 {query, intent, current_requirements, patterns, sections, evidences,
              enterprise_facts, answer, trace, context}。
        """
        # 1. 意图识别
        intent = QueryUnderstanding().classify(query)
        effective_types = list(pattern_types) if pattern_types else list(intent.pattern_types)

        # 2. 当前招标要求(可选)
        reqs, scores = self._resolve_requirements(requirements, score_items, tender_text)

        # 3. 双通道召回
        retrieved = self._retriever.retrieve(
            query,
            top_k=chunk_top_k,
            pattern_top_k=pattern_top_k,
            document_types=document_types,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            pattern_types=effective_types or None,
            rerank=rerank,
        )
        patterns = self._enrich_pattern_sources(retrieved["patterns"])
        evidences = retrieved["evidences"]

        # 4. fingerprint 去重 + pattern 知识节点(source 扩到兄弟章节)
        if deduplicate:
            patterns = self._dedupe_patterns(patterns)
        patterns = self._expand_pattern_sources(patterns)

        # 5. Section Expansion(chunk → 章节体系)
        sections: list[dict[str, Any]] = []
        if section_expand:
            sec_ids = [e.get("section_id") for e in evidences if e.get("section_id") is not None]
            sections = SectionRetriever().expand(sec_ids)

        # 6. 企业事实层(document_type 定向检索)
        enterprise_facts = self.search(
            query,
            top_k=settings.enterprise_fact_top_k,
            document_types=settings.enterprise_fact_document_types,
            enterprise_id=enterprise_id,
            knowledge_base_id=knowledge_base_id,
            rerank=rerank,
        )

        # 7. 四层 Evidence Pack
        pack = self.build_evidence_pack(
            query,
            patterns,
            evidences,
            sections=sections,
            enterprise_facts=enterprise_facts,
            current_requirements=reqs,
            score_items=scores,
        )
        context = self._format_pack(pack)

        # 8. LLM 生成
        from app.llm.gateway import get_llm

        llm = get_llm()
        prompt = f"【参考资料】\n{context}\n\n【用户问题】\n{query}"
        answer = llm.complete(prompt, system=system_prompt or _DEFAULT_RAG_SYSTEM)

        # 9. trace + 响应
        return {
            "query": query,
            "intent": intent.model_dump(),
            "current_requirements": reqs,
            "patterns": patterns,
            "sections": sections,
            "evidences": evidences,
            "enterprise_facts": enterprise_facts,
            "answer": answer,
            "trace": {
                "pattern_ids": [p.get("pattern_id") for p in patterns],
                "document_ids": sorted(
                    {e.get("document_id") for e in evidences if e.get("document_id") is not None}
                ),
                "enterprise_fact_document_ids": sorted(
                    {f.get("document_id") for f in enterprise_facts if f.get("document_id") is not None}
                ),
                "section_ids": [s.get("id") for s in sections],
                "chunk_ids": [e.get("id") for e in evidences],
            },
            "context": context,
        }

    @staticmethod
    def _resolve_requirements(
        requirements: list[dict[str, Any]] | None,
        score_items: list[dict[str, Any]] | None,
        tender_text: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        reqs = list(requirements) if requirements else []
        scores = list(score_items) if score_items else []
        if tender_text and (not reqs or not scores):
            from app.tender.requirement import RequirementExtractor
            from app.tender.score import ScoreExtractor

            if not reqs:
                reqs = [r.model_dump() for r in RequirementExtractor().extract(tender_text)]
            if not scores:
                scores = [s.model_dump() for s in ScoreExtractor().extract(tender_text)]
        return reqs, scores
