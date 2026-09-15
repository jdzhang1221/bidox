"""RAG V2 API 测试:对 POST /knowledge/rag 跑多组场景,验证意图识别/类型过滤/章节体系/trace。

用法: uv run python scripts/test_rag_api.py [base_url]
默认 base_url=http://localhost:8000
"""

from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
RAG = f"{BASE}/api/bidox/knowledge/rag"
# tenantId 是必填字段(缺失/冲突会被拒),所有用例都跑在同一租户内。
TENANT_ID = 1

# 复用:当前招标约束(对应 /tender/analyze 的 Requirement/ScoreItem 出参)
REQUIREMENTS = [
    {"code": "R001", "category": "技术要求", "description": "投标人须具备会计师事务所执业资格，项目负责人须为注册会计师", "mandatory": True, "page": 12},
    {"code": "R002", "category": "商务要求", "description": "服务期自合同签订之日起1年，审计报告须在收到通知后90日历天内出具", "mandatory": True, "page": 15},
]
SCORE_ITEMS = [
    {"code": "S001", "item": "技术方案", "score": 40, "criteria": "方案科学合理、可操作性强，质量控制措施完善", "page": 20},
    {"code": "S002", "item": "质量管控措施", "score": 20, "criteria": "全过程质量管控体系完整、责任明确", "page": 21},
]
TENDER_TEXT = (
    "投标人须具备会计师事务所执业资格，项目负责人须为注册会计师。"
    "服务期自合同签订之日起1年。"
    "评分标准：技术方案40分，要求方案科学合理、可操作性强；质量管控措施20分，要求全过程质量管控体系完整。"
)

# (场景名, 请求体)
CASES: list[tuple[str, dict]] = [
    # 1) 最小入参:只传 query + 归属,其余走默认(意图自动识别 + 默认检索)
    ("最小入参(默认配置)", {"query": "如何编写本项目的全过程质量管控措施？", "enterprise_id": 1, "knowledge_base_id": 1001}),
    # 2) 完整 retrieval 配置:显式调 top_k / section_expand / deduplicate
    ("完整检索配置", {
        "query": "如何编写本项目的进度保障措施？",
        "enterprise_id": 1,
        "knowledge_base_id": 1001,
        "retrieval": {"pattern_top_k": 3, "chunk_top_k": 8, "section_expand": True, "deduplicate": True, "rerank": True},
    }),
    # 3) 显式 filters.pattern_types 强过滤:只召回 quality_system 类组件
    ("显式类型强过滤", {
        "query": "质量管控",
        "enterprise_id": 1,
        "knowledge_base_id": 1001,
        "filters": {"pattern_types": ["quality_system"]},
    }),
    # 4) 当前招标要求层:直接传结构化的 requirements + score_items
    ("当前招标要求(结构化入参)", {
        "query": "如何编写本项目的全过程质量管控措施？",
        "enterprise_id": 1,
        "knowledge_base_id": 1001,
        "requirements": REQUIREMENTS,
        "score_items": SCORE_ITEMS,
    }),
    # 5) tender_text 现场提取:不传 requirements,由服务端从招标原文提取
    ("tender_text 现场提取", {
        "query": "如何编写本项目的全过程质量管控措施？",
        "enterprise_id": 1,
        "knowledge_base_id": 1001,
        "tender_text": TENDER_TEXT,
    }),
    # 6) document_types 定向检索:限定证据通道的文档类型
    ("document_types 定向检索", {
        "query": "如何编写本项目的全过程质量管控措施？",
        "enterprise_id": 1,
        "knowledge_base_id": 1001,
        "document_types": ["historical_bid"],
    }),
]


def run(name: str, body: dict) -> None:
    print(f"\n{'=' * 70}\n【{name}】")
    print("  query:", body["query"])
    if body.get("filters"):
        print("  filters:", body["filters"])
    try:
        r = httpx.post(RAG, json=body, timeout=180)
    except httpx.HTTPError as exc:
        print(f"  ❌ 请求失败: {exc}")
        return
    print("  HTTP", r.status_code)
    if r.status_code != 200:
        print("  body:", r.text[:300])
        return
    d = r.json().get("data", {})
    intent = d.get("intent", {})
    print(f"  intent: domains={intent.get('domains')} types={intent.get('pattern_types')} task={intent.get('task')}")
    print("  patterns(top3):")
    for p in d.get("patterns", [])[:3]:
        print(
            f"    {p.get('code'):6} type={p.get('type'):16} score={p.get('score')} "
            f"dup={p.get('duplicate_count', 1)} src={len(p.get('sources', []))}  {p.get('name')}"
        )
    print(f"  sections={len(d.get('sections', []))}  evidences={len(d.get('evidences', []))}")
    print(f"  current_requirements={len(d.get('current_requirements', []))}  "
          f"enterprise_facts={len(d.get('enterprise_facts', []))}")
    tr = d.get("trace", {})
    print(f"  trace: pattern_ids={tr.get('pattern_ids')} document_ids={tr.get('document_ids')} "
          f"section_ids={len(tr.get('section_ids', []))}个 chunk_ids={len(tr.get('chunk_ids', []))}个")
    ans = d.get("answer") or ""
    print(f"  answer(head 120): {(ans[:120]).replace(chr(10), ' ')}")


def main() -> None:
    print("RAG API 测试 →", RAG)
    for name, body in CASES:
        run(name, {"tenantId": TENANT_ID, **body})
    print(f"\n{'=' * 70}\n测试完成")


if __name__ == "__main__":
    main()
