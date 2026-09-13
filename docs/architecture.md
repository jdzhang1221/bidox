# BidOx 知识库与 RAG V2 数据模型设计

> 目标：支撑「一个企业上传数十~上百份历史标书，并把企业资质/案例/人员/制度等知识统一纳入 RAG」。
> 本文档是 BidOx RAG V2 的基础数据模型，**6 张表与检索链路均已落地**（实现见
> [app/models/](../app/models/)、[app/retrieval/](../app/retrieval/)、[app/knowledge/service.py](../app/knowledge/service.py)）。

## 一、设计原则

**一句话总纲：**

> Document 管「来源」，Section 管「结构」，Chunk 管「检索」，Pattern 管「复用」，Evidence 管「生成依据」，KnowledgeBase 管「边界」。

六种角色各司其职，不混用：

| 实体 | 角色 | 是否向量化 | Embedding 内容 |
|------|------|-----------|---------------|
| Document | 知识来源 | ❌ | — |
| Section | 知识结构/溯源 | ❌（暂不） | — |
| Chunk | 检索证据 | ✅ | `content` |
| Pattern | 可复用方案 | ✅ | `name+type+industry+scenario+summary+structure` |
| PatternSource | 方案溯源 | ❌ | — |
| KnowledgeBase | 企业知识边界 | ❌ | — |

## 二、整体 ER

```text
                    knowledge_base (知识库)
                         │
             ┌───────────┴───────────┐
             │ 1:N                   │ 1:N
             ▼                       ▼
         document                solution_pattern
             │ 1:N                   │
             ▼                       │ 1:N
       document_section             │
             │ 1:N                   ▼
             ▼                  pattern_source ────┐
       document_chunk ◀─────────────────────────────┤ (source 引用 document/section/chunk)
             │                                       │
             ▼                                       │
        embedding (pgvector) ◀───────────────────────┘
```

- 左链（document → section → chunk）描述**「这份标书原来是什么样的」**（来源/结构/证据）。
- 右链（document → section/chunk → pattern）描述**「这份标书里有哪些东西值得复用」**（派生知识）。
- Pattern 不是 Section 的替代品，而是「从一个或多个 Section/Chunk 提炼出的可复用知识」。

## 三、表设计（MVP = 6 张表）

### 1. `knowledge_base`（新增）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BigInteger PK | |
| tenant_id | BigInteger, index | 租户 |
| enterprise_id | BigInteger, index | 企业 |
| name | String(256) | 知识库名 |
| description | Text, null | 描述 |
| kind | String(64), index | `bid_library` / `enterprise_library` / `qualification_library` / `case_library` / `solution_library` |
| created_at / updated_at | DateTime(tz) | |

> MVP 用 `kind` 区分逻辑库，**不建多张物理库表**。

### 2. `document`（在现有 `DocumentRecord` 上增改）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BigInteger PK | |
| tenant_id / enterprise_id / project_id | BigInteger, index | 归属 |
| **knowledge_base_id** | BigInteger, index, 新增 | 所属知识库 |
| **name** | String(512), 新增 | 文档标题（展示用），区别于文件名 |
| file_name | String(512) | 原文件名 |
| file_type | String(32) | `pdf` / `docx` / `doc` |
| file_size | BigInteger, null | |
| **storage_key** | String(1024) | 对象存储 key（原 `storage_path` 更名） |
| **file_hash** | String(64), index | SHA256，**去重键**（取代 md5/sha256 双列） |
| document_type | String(64), index | 见下方枚举 |
| status | String(32) | `pending`→`parsing`→`parsed`→`embedded`→`failed` |
| **parser** | String(32), 新增 | 解析器：`docx` / `pdf_text` / `ocr` / `mineru` |
| **parser_version** | String(32), 新增 | 解析器版本（便于重解析） |
| page_count | Integer, null | |
| created_at / updated_at | DateTime(tz) | |

**`document_type` 统一枚举**（承载「历史标书/企业资料/资质/案例/技术文档」各类知识，避免为每类单独建表）：

```text
historical_bid / tender / enterprise_profile / qualification /
project_case / product_manual / technical_document / regulation / other
```

### 3. `document_section`（增改）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BigInteger PK | |
| document_id | BigInteger, index | |
| **knowledge_base_id** | BigInteger, index, 新增 | 冗余，检索免 join |
| parent_id | BigInteger, index | 自引用成树，null=顶层 |
| section_no | String(64), null | 如 `5.1.2` |
| title | String(512) | |
| level | Integer | 层级 |
| section_type | String(64), index | |
| content | Text, null | |
| page_start / page_end | Integer, null | |
| sort_order | Integer | |
| **metadata** | JSONB, null, 新增 | 如 `is_cover` / `is_form` / `is_toc` |
| created_at | DateTime(tz) | |

> Section 职责只有一个：保存原始文档结构。**不向量化**（第一优先级）。

### 4. `document_chunk`（增改）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BigInteger PK | |
| tenant_id / enterprise_id / knowledge_base_id | BigInteger, index | 归属（冗余） |
| document_id | BigInteger, index | |
| section_id | BigInteger, index | 来源章节（已回填） |
| chunk_index | Integer | |
| **chunk_type** | String(32), 新增 | `paragraph` / `table` / `list` / `heading` / `mixed` |
| title | String(512), null | |
| content | Text | |
| document_type / section_type | String(64), index | 冗余，过滤用 |
| page_start / page_end | Integer, null | |
| metadata | JSONB | 是否表格、原始块类型等 |
| embedding | vector(1024) | ✅ 只 embed `content` |
| created_at | DateTime(tz) | |

> `chunk_type` 关键：评分标准表/技术参数表/人员配置表不能当普通文本切，需保持表格完整。

### 5. `solution_pattern`（新增，从 pydantic 升为表）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BigInteger PK | |
| tenant_id / enterprise_id / knowledge_base_id | BigInteger, index | 归属（冗余） |
| name | String(512) | 通用化组件名 |
| code | String(64) | 人类可读编号（KB 内唯一） |
| type | String(64), index | `quality_system` / `schedule` / … |
| industry | String(128), null | |
| scenario | String(256), null | |
| summary | Text | **用于检索** |
| structure | JSONB | **用于方案理解**（子模块列表） |
| content | Text | 章节全文（原始，无损保留） |
| **generalized_content** | Text, null | **去事实化正文**（LLM 预生成）：去掉历史项目具体数字/机构名/人名，方案复用时不污染新标书 |
| fingerprint | String(64), index | 内容指纹，**跨文档去重** |
| version | Integer, default 1 | |
| status | String(32), default `active` | `active` / `draft` / `archived` |
| embedding | vector(1024) | ✅ embed `name+type+industry+scenario+summary+structure` |
| created_at / updated_at | DateTime(tz) | |

> 溯源不放在 pattern 自身，统一走 `pattern_source`（一个 pattern 可来自多个 section/chunk）。

### 6. `pattern_source`（新增）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BigInteger PK | |
| pattern_id | BigInteger, index | FK → solution_pattern |
| document_id | BigInteger, index | 来源文档 |
| section_id | BigInteger, index, null | 来源章节 |
| chunk_id | BigInteger, index, null | 来源切片（更细粒度时用） |
| source_type | String(32) | `section` / `chunk` |
| page_start / page_end | Integer, null | 该来源的页码 |
| sort_order | Integer | 多来源排序 |
| created_at | DateTime(tz) | |

> 为什么用关联表而不是 `source_section_id` 单列：一个 Pattern 常来自多个历史标书的多个章节（如「三级复核」在 100 份标书出现 127 次），N:N 溯源是多历史标书场景的硬需求，也是「生成依据可追溯回具体标书哪一章」的前提。

## 四、向量化策略（不「一锅端」）

| 对象 | 是否向量化 | Embedding 内容 | 作用 |
|------|-----------|---------------|------|
| Document | ❌ | — | 管理 |
| Section | 暂不 | — | 结构 |
| Chunk | ✅ | `content` | 精确证据 |
| Pattern | ✅ | 见下 | 方案召回 |

**Pattern embedding_text 构造**（不只用 summary）：

```text
名称：{name}
类型：{type}
行业：{industry}
场景：{scenario}
摘要：{summary}
结构：{structure 逐条}
```

把 `name/type/industry/scenario/summary/structure` 拼接后整体 embedding，比单纯 `embedding(summary)` 更适合方案级召回。

## 五、RAG V2 检索链路（已实现）

```text
                      用户问题 / 评分点
                            │
                            ▼
                     Query Understanding          (规则:领域→pattern_types→task)
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
        Pattern Retrieval             Chunk Retrieval
        (向量 + type 强过滤)           (向量 + 关键词, RRF)
              │                           │
              ▼                           ▼
        recall-then-rerank             recall-then-rerank
        (多召回 20 → 重排 Top5)         (RRF 后重排 Top10)
              │                           │
              ▼                           ▼
     fingerprint 去重 + 多文档聚合      Section Expansion
     pattern 知识节点(扩兄弟章节)       (命中→父→兄弟)
              │                           │
              └─────────────┬─────────────┘
                            ▼
              企业事实层(document_type 定向召回)
                            │
                            ▼
              四层 Evidence Pack + 事实脱敏
                            │
                            ▼
                           LLM
                            │
                            ▼
                        标书章节
```

- **Pattern 通道**回答「应该怎么设计」（方案）：召回先按意图做 `pattern_types` 强过滤（解决串题），
  再 `recall-then-rerank`（多召回 `reranker_recall_k=20` 条 → reranker 重排到 Top5），把「语义相似 ≠ 业务相关」的噪声压下去。
- **Chunk 通道**回答「过去别人具体怎么写」（历史证据）：向量 + 关键词 RRF 融合后再 reranker 重排。
- **知识节点**：每个 pattern 的 source 扩到「命中 → 父 → 兄弟」章节，SP009「全过程质量管控」会关联
  质量管理体系/质量目标/质量责任追究等完整方案体系，而非单章节别名。
- **企业事实层**：按 `document_type ∈ {enterprise_profile, qualification, project_case}` 定向召回，
  作为真实资质/人员/案例注入（当前无企业文档时为空的就绪链路）。

**四层 Evidence Pack**（LLM 输入，结构化分层而非碎片）：

```json
{
  "query": "全过程质量管控措施",
  "current_requirements": [ { "code": "R001", "category": "技术要求", "description": "..." } ],
  "patterns": [
    {
      "code": "SP009", "name": "全过程质量管控措施", "type": "quality_system",
      "summary": "...", "structure": ["事前质量管控", "事中质量管控", "事后质量管控"],
      "generalized_content": "（去事实化正文，注入 LLM 用这个而非 content）",
      "sources": [ { "section_id": 249 }, { "section_id": 250, "source_type": "related" } ]
    }
  ],
  "evidences": [ { "document_id": 2001, "section_id": 244, "content": "（脱敏后正文）" } ],
  "enterprise_facts": []
}
```

**事实隔离（防历史事实污染新标书）**——两条硬措施：

1. **方案层**：注入 `generalized_content`（LLM 预生成的去事实化正文），**不注入原始 `content`**。
2. **证据层**：注入前对 chunk 原文做确定性脱敏（数字+单位→「某」、机构名→「某机构」、日期→「某年某月某日」），
   历史证据只保留「写法与结构」，具体数字/机构名/人名不得进入新标书。

## 六、多文档去重

100 份标书 → 可能 3000 个 Pattern，其中「三级复核」出现 127 次。不能让 RAG 返回 5 个雷同组件。

策略（**惰性去重**，不做入库即合并）：

1. 入库时给每个 pattern 算 `fingerprint`（通用化内容哈希）。
2. 召回后按 `fingerprint` 分组，每组返回 1 个代表 Pattern + 该组全部 Evidence Source：

```text
三级质量复核体系
├── 历史案例 A（DOC001 / SEC001）
├── 历史案例 B（DOC003 / SEC2051）
└── 历史案例 C（DOC007 / SEC3012）
```

> 保留每个 source 的 document_id/section_id，保证溯源不丢（审计/合规硬需求）；过早合并成 canonical pattern 会丢溯源。

## 七、多租户隔离（红线）

- 所有向量表（chunk / pattern）带 `enterprise_id` + `knowledge_base_id`。
- **每条检索 SQL 强制带 `enterprise_id`（或 `knowledge_base_id`）谓词**。
- 组合索引：`(enterprise_id, knowledge_base_id)`、`(knowledge_base_id, type)`、`(knowledge_base_id, document_type)`。

## 八、分阶段落地

- **P0（打通核心链路）✅ 已落地**：`knowledge_base` + `document.knowledge_base_id` + `solution_pattern` / `pattern_source` 建表；入库 API 接受 `knowledge_base_id`，一次入库同时产出 chunk + pattern，全链路关联。
- **P1（召回质量）✅ 已落地**：pattern fingerprint 去重 + 双通道检索 + 四层 Evidence Pack 组装 + 本地 reranker（bge-reranker-v2-m3）+ 事实隔离（generalized_content + 证据层脱敏）。
- **P2（进阶，未开始）**：canonical pattern 合并、`pattern_relation`（prerequisite/complementary/derived_from，等积累 100+ Pattern 后再根据共现数据自动建）、section 向量化（若需要中级粒度）、enterprise_case / enterprise_knowledge 拆表（若需要特殊结构化字段）。

## 九、与现有代码的映射 / 迁移点

> 下表迁移点已全部完成。

| 现状（旧） | 目标（已落地） |
|------|------|
| `document` 无 knowledge_base_id | ✅ knowledge_base_id、name、parser、parser_version 已加 |
| `document.storage_path / md5 / sha256` | ✅ 更名/合并为 `storage_key` + `file_hash`(sha256) |
| `document_chunk.section_id` 曾为 NULL | ✅ 已回填 |
| `document_chunk` 无 chunk_type | ✅ 已加 chunk_type |
| `app/pattern/models.py`（pydantic） | ✅ 升为 `solution_pattern` + `pattern_source` 表 |
| `pattern.source`（弱 dict） | ✅ 由 `pattern_source` 关联表取代 |
| `pattern_relation`（抽取脚本产物） | ⏳ 延迟到 P2 |
