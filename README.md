# BidOx AI 标书生成助手

BidOx 的 AI 服务（独立 Python 服务），负责**文档智能解析 → 结构化 → 向量化 → 检索 → RAG → 招标分析 → 标书生成**。

与 Java 侧（`bidox-service`）的边界：Java 负责用户/租户/企业/项目/文件上传/业务库/MQ 投递；Python 负责解析、AST、Section、Chunk、Embedding、pgvector 检索、RAG、LLM。

## 技术栈

- **框架**：FastAPI + SQLAlchemy 2.0 + PostgreSQL + pgvector
- **向量模型**：BGE-M3（1024 维），支持本地 `FlagEmbedding` 或 `Ollama` HTTP 两种后端
- **Reranker**：本地 `BAAI/bge-reranker-v2-m3`（FlagEmbedding 交叉编码器）
- **LLM**：DeepSeek / Qwen / OpenAI 兼容网关
- **依赖管理**：[uv](https://docs.astral.sh/uv/)

## 架构总览

```
文件(上传,由 Java 投递 MQ / 或本地调试直传路径)
   │
   ▼
文件检测 / 类型识别 / SHA256 去重
   │
   ├── PDF ──┬── 电子 PDF  → pdf_inspector (PyMuPDF)
   │         ├── 扫描 PDF  → PaddleOCR
   │         └── 复杂 PDF  → MinerU 增强
   │
   └── Word ─┬── DOCX → python-docx + XML 增强
             └── DOC  → LibreOffice → DOCX
   │
   ▼
Document AST (统一结构:heading/paragraph/table/list/image)
   │
   ▼
Section 识别 + 分类 (标题层级 → 章节树)
   │
   ├── Solution Pattern 抽取 (方案组件 + 去事实化 generalized_content)
   │
   └── Chunk (标题层级 + 语义 + 表格完整性综合切分)
         │
         ├── Metadata → PostgreSQL
         └── Embedding → pgvector (1024 维)
   │
   ▼
RAG V2 检索链路 (详见 docs/architecture.md)
   Query Understanding → 双通道召回 → recall-then-rerank
   → fingerprint 去重 / 知识节点 → Section Expansion → 企业事实层
   → 四层 Evidence Pack + 事实脱敏 → LLM
   │
   ▼
LLM → 招标要求提取 / 评分点提取 / 风险识别 / 标书生成
```

## 目录结构

```
bixox-ai/
├── app/
│   ├── core/              # 配置、日志、DB、存储、工具
│   ├── document/          # 文档解析流水线
│   │   ├── ast/           #   Document AST 模型与构建
│   │   ├── parser/        #   pdf/word 解析器 + factory
│   │   ├── section/       #   章节识别 + 分类
│   │   ├── chunk/         #   切分(语义/表格)
│   │   └── pipeline.py    #   解析总流水线
│   ├── models/            # ORM 表:document / section / chunk / pattern / knowledge_base
│   ├── embedding/         # BGE-M3 向量化(local / ollama 两后端)
│   ├── retrieval/         # 检索链路:query(意图) / vector / keyword / pattern / section / reranker / hybrid
│   ├── knowledge/         # 知识库:ingest(入库) / indexer(索引) / service(RAG V2 编排)
│   ├── pattern/           # 方案组件抽取 + 入库(含 generalized_content)
│   ├── tender/            # 招标要求/评分/风险结构化提取
│   ├── llm/               # LLM 网关(deepseek/qwen/openai)
│   ├── tasks/             # MQ 任务消费者
│   └── api/               # FastAPI 路由(document/knowledge/retrieval/tender)
├── scripts/               # 本地端到端脚本(入库 / 抽取 / 迁移 / 验证 / API 测试)
├── tests/
├── docs/architecture.md   # 数据模型 + RAG V2 检索链路设计
├── docker-compose.yml
└── pyproject.toml
```

## 快速开始

> 依赖用 [uv](https://docs.astral.sh/uv/) 管理。安装 uv：`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`（或 `pip install uv`）。

```bash
# 1. 启动基础设施(PostgreSQL + pgvector + RabbitMQ + MinIO)
docker compose up -d

# 2. 安装依赖(核心 + 开发依赖),自动创建 .venv
uv sync

# 3. 可选重型依赖
uv sync --extra embedding --extra reranker   # 本地向量化 + reranker(CPU 可用)
uv sync --extra ocr --extra mineru           # OCR / MinerU

# 4. 配置
cp .env.example .env

# 5. 启动 API(前缀 /api/bidox)
uv run uvicorn app.main:app --reload --port 8000

# 6. 启动 MQ 消费者(生产走异步)
uv run python -m app.tasks.consumer --queue parse
```

> 向量化后端由 `EMBEDDING_PROVIDER` 决定：`local`（进程内加载 BGE-M3）或 `ollama`（走 Ollama HTTP，模型 `bge-m3`）。
> Reranker 由 `RERANKER_ENABLED` 开关，首次会下载 `bge-reranker-v2-m3`（约 1.5GB）。

## API 一览（前缀 `/api/bidox`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/`、`/health` | 健康检查 |
| POST | `/document/parse` | 同步解析文档（`persist=true` 时向量化落库） |
| POST | `/document/parse-local` | 解析本地文件（调试用，直传本地路径） |
| POST | `/retrieval/search` | 混合检索（历史证据 chunk，向量+关键词+reranker） |
| POST | `/retrieval/patterns` | 方案组件检索（含 source 溯源） |
| POST | `/knowledge/context` | 拼装 evidence context |
| POST | `/knowledge/generate` | RAG 生成（双通道一步到位，兼容旧出参） |
| POST | `/knowledge/rag` | **RAG V2 全链路**（四层 Evidence Pack + reranker + 事实隔离） |
| POST | `/tender/analyze` | 招标文件结构化分析（要求/评分/风险，输入纯文本） |
| POST | `/tender/analyze-local` | 本地招标文件分析（直接传路径，内部解析→提取） |

### `/knowledge/rag` 请求示例

```json
{
  "query": "如何编写本项目的全过程质量管控措施？",
  "enterprise_id": 1,
  "knowledge_base_id": 1001,
  "retrieval": { "pattern_top_k": 5, "chunk_top_k": 10, "section_expand": true, "deduplicate": true, "rerank": true },
  "filters": { "pattern_types": ["quality_system"] },
  "requirements": [ { "code": "R001", "category": "技术要求", "description": "项目负责人须为注册会计师" } ],
  "score_items": [ { "code": "S001", "item": "技术方案", "score": 40, "criteria": "方案科学合理" } ]
}
```

返回四层 `{ current_requirements, patterns, sections, evidences, enterprise_facts, answer, trace, context }`。

## 关键设计

- **统一 Document AST**：所有解析器（PDF/Word/OCR/MinerU）都输出同一种 AST，后续能力全部基于 AST。
- **表格完整性**：表格作为一个完整 Chunk，不拆散，保证评分项与其分值/标准不割裂。
- **保留 page**：chunk/section 记录 `page_start/page_end`，支撑「生成依据」可追溯。
- **懒加载重依赖**：OCR/MinerU/Embedding/Reranker 未安装时降级为 stub 并告警，不阻塞主链路。
- **六角色数据模型**：`Document`（来源）→ `Section`（结构）→ `Chunk`（检索）→ `Pattern`（复用）→ `PatternSource`（溯源）→ `KnowledgeBase`（边界），详见 [docs/architecture.md](docs/architecture.md)。
- **四层 Evidence Pack**：`当前招标要求 / 方案组件 / 历史证据 / 企业事实` 分层隔离后注入 LLM，而非碎片拼接。
- **事实隔离（防历史事实污染）**：方案层注入 `generalized_content`（去事实化正文，不注入原文）；证据层注入前做确定性脱敏（数字/机构名/日期 → 占位），历史标书里的「11 家 / 90 日历天 / 洛阳市总工会」等具体事实不会写进新标书。
- **方案组件（Solution Pattern）**：历史标书里真正可复用的是一个「方案单元」（如九阶段审计流程、三级复核质量体系）。以章节为边界，保存完整上下文 + 通用化 summary/structure + 去事实化 generalized_content。
- **知识节点**：每个 Pattern 的 source 扩到「命中 → 父 → 兄弟」章节，SP009「全过程质量管控」关联质量管理体系/质量目标/质量责任追究等完整方案体系。

## 脚本

| 脚本 | 用途 |
|------|------|
| `scripts/run_local_docx.py` | 本地解析 DOCX（走完整流水线） |
| `scripts/ingest_doc.py` | 解析 + 入库（document/chunk/pattern 全链路） |
| `scripts/extract_solution_patterns.py` | 离线抽取方案组件 |
| `scripts/regeneralize_patterns.py` | 迁移：回填已有 pattern 的 `generalized_content`（幂等） |
| `scripts/verify_p0_ingest.py` | 验证 P0 入库链路 |
| `scripts/verify_rag_v2.py` | 验证 RAG V2（意图/重排/事实隔离/知识节点/四层结构） |
| `scripts/test_rag_api.py` | 对 `POST /knowledge/rag` 跑多组 API 测试 |

```bash
uv run python scripts/regeneralize_patterns.py   # 回填去事实化正文
uv run python scripts/verify_rag_v2.py           # RAG V2 端到端验证
uv run python scripts/test_rag_api.py            # API 测试(默认 http://localhost:8000)
uv run python -m pytest tests/                   # 回归测试
```

## 开发阶段（建议）

- **Phase 1**：DOCX → AST → Section → Chunk → Embedding → pgvector（先拿历史标书跑通）✅
- **Phase 2**：PDF → pdf_inspector，再加 MinerU / PaddleOCR
- **Phase 3**：招标 PDF → LLM 提取要求/评分/风险 ✅（`/tender/analyze`）
- **Phase 4**：RAG + 标书生成 + AI 审核 + Word 导出（RAG V2 已打通，生成/导出待扩展）
