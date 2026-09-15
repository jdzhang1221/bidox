"""RAG V2 检索链路验证(串题修复 + 章节体系 + 去重 + 事实隔离 + pattern 知识节点)。

用法: uv run python scripts/verify_rag_v2.py
注意: 需先运行 scripts/regeneralize_patterns.py 回填 generalized_content。
"""

from __future__ import annotations

from app.core.config import settings
from app.knowledge.service import KnowledgeService
from app.retrieval.query import QueryUnderstanding

QUERY = "如何编写本项目的全过程质量管控措施？"
TENANT_ID = 1
ENTERPRISE_ID = 1
KB_ID = 1001
# 历史标书里的具体事实,新标书里绝不能出现
FORBIDDEN_FACTS = ["11家", "11 家", "90日历天", "90 日历天", "洛阳市总工会", "河南金冕"]


def main() -> None:
    svc = KnowledgeService()

    # 1. 意图识别
    intent = QueryUnderstanding().classify(QUERY)
    print("=== 意图识别 ===")
    print(intent.model_dump())
    assert "quality" in intent.domains and intent.pattern_types

    # 2. Pattern 召回(type 过滤 + reranker 重排)
    print("\n=== Pattern 召回 ===")
    patterns = svc._retriever.search_patterns(
        QUERY, tenant_id=TENANT_ID, top_k=5, enterprise_id=ENTERPRISE_ID, knowledge_base_id=KB_ID,
        pattern_types=intent.pattern_types,
    )
    for p in patterns:
        print(f"  {p['code']:6} type={p['type']:16} score={p['score']}[{p.get('score_type')}]  {p['name']}")
    codes = [p["code"] for p in patterns]
    assert any("质量" in p["name"] for p in patterns), "应命中质量类方案"
    assert "SP010" not in codes and "SP011" not in codes, "进度类方案应被 type 过滤排除"
    if settings.reranker_enabled and len(patterns) > 1:
        assert all(p["score_type"] == "rerank" for p in patterns), (
            "启用 reranker 时,pattern 应经重排(score_type=rerank),而非原始 cosine"
        )

    # 3. 事实隔离:generalized_content 非空
    print("\n=== 事实隔离(generalized_content) ===")
    for p in patterns:
        g = p.get("generalized_content") or ""
        assert g, f"{p['code']} 缺少 generalized_content(请先跑 regeneralize_patterns.py)"
        leaked = [f for f in FORBIDDEN_FACTS if f in g]
        print(f"  {p['code']} generalized_content={len(g)}字 泄漏事实={leaked or '无'}")
        assert not leaked, f"{p['code']} 去事实化正文仍含历史事实 {leaked}"

    # 4. Pattern 知识节点:source 扩到兄弟章节
    print("\n=== Pattern 知识节点(source 扩到兄弟章节) ===")
    enriched = svc._enrich_pattern_sources(patterns)
    expanded = svc._expand_pattern_sources(enriched)
    for p in expanded:
        srcs = p.get("sources", [])
        rel = [s for s in srcs if s.get("source_type") == "related"]
        print(f"  {p['code']} 总source={len(srcs)} 兄弟章节={len(rel)}")
    assert any(
        len([s for s in p.get("sources", []) if s.get("source_type") == "related"]) > 0
        for p in expanded
    ), "应有 pattern 关联到兄弟章节"

    # 5. 完整 rag():四层结构 + 事实隔离
    print("\n=== rag() 四层结构 ===")
    result = svc.rag(QUERY, tenant_id=TENANT_ID, enterprise_id=ENTERPRISE_ID, knowledge_base_id=KB_ID,
                     chunk_top_k=10, pattern_top_k=5)
    print("  keys:", sorted(result.keys()))
    for k in ("current_requirements", "patterns", "sections", "evidences", "enterprise_facts", "trace"):
        assert k in result, f"缺字段 {k}"
    assert "enterprise_fact_document_ids" in result["trace"]
    ans = result["answer"] or ""
    leaked = [f for f in FORBIDDEN_FACTS if f in ans]
    print(f"  answer 泄漏历史事实 = {leaked or '无'}")
    assert not leaked, f"最终答案仍含历史事实 {leaked}"

    print("\n[PASS] RAG V2 验证通过(串题修复 + 章节体系 + 去重 + 事实隔离 + 知识节点 + 四层结构)")


if __name__ == "__main__":
    main()
