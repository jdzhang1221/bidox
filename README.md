# BidOx AI · 标书生成 AI 服务

**BidOx（数牛智标）** 是面向招投标场景的 AI 写标书 SaaS 平台，业务主链路为：

> 招标文件理解 → 评分点拆解 → 企业知识匹配 → AI 标书生成 → 评分点审核

`bixox-ai` 是平台三端之一，承担 **AI 服务** 职责（独立 Python 服务），负责 **文档智能解析 → 结构化 → 向量化 → 检索 → RAG → 招标分析 → 标书生成**。

> 目录名 `bixox-ai` 是历史笔误，包名与正确名称为 **`bidox-ai`**（见 `pyproject.toml`）。

> **当前阶段：核心 AI 链路已打通，且已与 Java 后端、前端三端联调完成。** 详见 [当前进展](#当前进展)。

## 技术栈

| 项 | 说明 | 版本 |
| --- | --- | --- |
| Python | 运行时 | `>= 3.11`（本机 venv 为 3.12.6） |
| [uv](https://docs.astral.sh/uv/) | 依赖与虚拟环境管理 | `uv.lock` |
| FastAPI | Web 框架 | `>= 0.110` |
| SQLAlchemy | ORM | `>= 2.0` |
| PostgreSQL + pgvector | 向量库 | `pgvector >= 0.2.5` |
| Pydantic / pydantic-settings | 校验与配置 | `>= 2.7` / `>= 2.2` |
| 文档解析 | python-docx / pypdf / PyMuPDF | `>= 1.1` / `>= 4.2` / `>= 1.24` |
| HTTP 客户端 | httpx | `>= 0.27` |
| 向量模型 | BGE-M3（1024 维），支持本地 `FlagEmbedding` 或 `Ollama` | - |
| Reranker | `BAAI/bge-reranker-v2-m3`（默认关闭） | - |
| LLM | DeepSeek / Qwen / OpenAI 兼容网关 | - |
| 测试 | pytest | `tests/` 6 个文件 1712 行 |

## 目录结构

```
bixox-ai/
├── app/
│   ├── core/              # 配置、日志、DB、存储抽象、租户、文档类型、有界线程池
│   ├── document/          # 文档解析流水线
│   │   ├── ast/           #   Document AST 模型与构建
│   │   ├── parser/        #   pdf(pdf_inspector/ocr/mineru) + word(docx/doc/xml) + factory
│   │   ├── section/       #   章节识别(detector) + 分类(classifier)
│   │   ├── chunk/         #   切分(semantic_chunker / splitter / table_chunker)
│   │   └── pipeline.py    #   解析总流水线
│   ├── models/            # ORM 8 张表（document / section / chunk / pattern / knowledge_base / index_guard）
│   ├── embedding/         # BGE-M3 向量化：base / bge_m3 / ollama / service
│   ├── retrieval/         # 检索链路：query(意图) / vector / keyword / pattern / section / reranker / hybrid
│   ├── knowledge/         # 知识库：ingest(入库) / indexer(索引) / guard(索引守卫) / service(RAG V2 编排) / qa_stream / qa_prompt
│   ├── pattern/           # 方案组件抽取 + 入库（含 generalized_content）
│   ├── tender/            # 招标要求 / 评分点 / 风险结构化提取
│   ├── llm/               # LLM 网关（base / gateway / openai_compat）
│   ├── tasks/             # MQ 任务消费者与任务实现（parse / embedding / analysis）
│   └── api/               # FastAPI 路由：router / document / knowledge / retrieval / tender / schemas
├── scripts/               # 本地端到端脚本（13 个：入库 / 抽取 / 迁移 / 验证 / API 测试）
├── tests/                 # 回归测试（6 个文件）
├── docs/architecture.md   # 数据模型 + RAG V2 检索链路设计
├── docker-compose.yml     # PostgreSQL(pgvector) + RabbitMQ + MinIO
├── pyproject.toml / uv.lock
└── LICENSE                # 专有许可（与 pyproject 的 Proprietary 一致）
```

## 快速开始

> 依赖用 [uv](https://docs.astral.sh/uv/) 管理。安装 uv：`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`（或 `pip install uv`）。

```bash
# 1. 启动基础设施（可选，见下方「外部依赖」）
docker compose up -d

# 2. 安装依赖（核心 + 开发依赖），自动创建 .venv
uv sync

# 3. 可选重型依赖
uv sync --extra embedding --extra reranker   # 本地向量化 + reranker（CPU 可用）
uv sync --extra ocr --extra mineru           # OCR / MinerU

# 4. 配置
cp .env.example .env
#   必填：LLM_API_KEY
#   按环境调整：DATABASE_URL（库 bidox_ai）、LOCAL_STORAGE_BASE_DIR

# 5. 启动 API（前缀 /api/bidox）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> ⚠️ **实际部署不带 `--reload`**：`uvicorn` 未开启热重载，**修改 Python 代码或 `.env` 后必须重启进程**，否则改动不生效。

启动后：健康检查 `http://127.0.0.1:8000/health`，Swagger `http://127.0.0.1:8000/docs`。

### 外部依赖

| 依赖 | 是否必须 | 说明 |
| --- | --- | --- |
| **PostgreSQL + pgvector** | ✅ 必须 | 库名 `bidox_ai`，本机端口 **5433**；检索与落库都依赖它 |
| **LLM API**（DeepSeek 等） | ✅ 必须 | RAG 生成、招标分析、方案组件抽取都要调用 |
| Ollama（11434） | ⚪ 可选 | `EMBEDDING_PROVIDER=ollama` 时需要；可切换为 `local` 走进程内 BGE-M3 |
| RabbitMQ（5672） | ⚪ 可选 | 仅 MQ 消费者需要；一期走同步 API |
| MinIO（9000） | ⚪ 可选 | 一期存储走本地磁盘（`STORAGE_PROVIDER=local`），**不需要 MinIO** |

> `docker-compose.yml` 里仍带着 RabbitMQ 与 MinIO，属于预留；一期只用其中的 PostgreSQL。
> 注意其 PostgreSQL 已映射为 **5433:5432**，与 `.env` 的 `DATABASE_URL` 及后端 `bidox-service` 的 local profile 对齐。

## API 一览（前缀 `/api/bidox`）

| 方法 | 路径 | 说明 | Java 调用 |
|------|------|------|:---:|
| GET | `/`、`/health` | 健康检查 | |
| POST | `/document/parse` | 同步解析文档（`persist=true` 时向量化落库） | ✅ |
| POST | `/document/parse-local` | 解析本地文件（调试用，直传本地路径） | |
| POST | `/document/delete` | 删除文档索引（幂等 tombstone） | ✅ |
| POST | `/knowledge/qa-stream` | **企业知识问答流式接口（SSE）** | ✅ |
| POST | `/knowledge/context` | 拼装 evidence context | |
| POST | `/knowledge/generate` | RAG 生成（双通道一步到位，兼容旧出参） | ✅ |
| POST | `/knowledge/rag` | **RAG V2 全链路**（四层 Evidence Pack + 事实隔离） | |
| POST | `/retrieval/search` | 混合检索（历史证据 chunk，向量 + 关键词 + reranker） | ✅ |
| POST | `/retrieval/patterns` | 方案组件检索（含 source 溯源） | |
| POST | `/tender/analyze` | 招标文件结构化分析（要求 / 评分 / 风险，输入纯文本） | ✅ |
| POST | `/tender/analyze-local` | 本地招标文件分析（直接传路径，内部解析 → 提取） | |

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

## 数据模型与存储契约

### 表（8 张，`app/models/`）

| 表 | 说明 |
| --- | --- |
| `document` | 文档主表 |
| `document_section` | 章节（保留 `page_start/page_end`，支撑「生成依据」可追溯） |
| `document_chunk` | 检索单元，`embedding Vector(1024)` |
| `solution_pattern` | 方案组件，含 `generalized_content` 与 embedding |
| `pattern_source` | 方案组件 → 来源章节溯源 |
| `knowledge_base` | 知识库边界 |
| `document_index_guard` | 索引守卫（防并发/重复建索引） |
| `document_pattern_job` | 方案组件抽取任务状态 |

- **Embedding 维度 1024**，模型 BGE-M3（`embedding/service.py` 按 `EMBEDDING_PROVIDER` 分派 `local`/`ollama`）。
- ⚠️ **未建 ivfflat / hnsw 向量索引**：检索走 `cosine_distance` 顺序扫描（`retrieval/vector.py`）。数据量上来后需补索引。

### 存储抽象（关键契约）

`app/core/storage.py` 的 `get_storage(provider=None)`：

| provider | 客户端 | 行为 |
| --- | --- | --- |
| `local` | `LocalClient` | 相对路径拼 `LOCAL_STORAGE_BASE_DIR`；绝对路径直用；兼容 http(s) 直链下载 |
| `minio` / `oss` / `s3` | `MinIOClient` | 懒加载 `minio` 包，`fget_object` 拉取 |
| 未指定 | 回退 `settings.storage_provider`，再回退「配了 MinIO 就用 MinIO」，否则 Local | |

> ⚠️ **Java → Python 文件契约**：Java 传 `storageProvider="local"` + `storageKey=<相对路径>`（如 `bid-document/20260914/x.docx`）。
> Python **必须按 `storageProvider` 分派存储客户端**，不能只看自己 `.env` 里的 `MINIO_*`，否则会把相对路径当 bucket/object 去连 MinIO:9000，报 `MaxRetryError` 且解析恒失败。
> `LOCAL_STORAGE_BASE_DIR` 必须等于 Java 的启动工作目录（当前为 `D:/snkj/BidOx/backend/bidox-service`）。

## 关键设计

- **统一 Document AST**：所有解析器（PDF / Word / OCR / MinerU）都输出同一种 AST，后续能力全部基于 AST。
- **表格完整性**：表格作为一个完整 Chunk，不拆散，保证评分项与其分值/标准不割裂。
- **保留 page**：chunk / section 记录 `page_start/page_end`，支撑「生成依据」可追溯。
- **懒加载重依赖**：OCR / MinerU / Embedding / Reranker 未安装时降级为 stub 并告警，不阻塞主链路。
- **六角色数据模型**：`Document`（来源）→ `Section`（结构）→ `Chunk`（检索）→ `Pattern`（复用）→ `PatternSource`（溯源）→ `KnowledgeBase`（边界），详见 [docs/architecture.md](docs/architecture.md)。
- **四层 Evidence Pack**：`当前招标要求 / 方案组件 / 历史证据 / 企业事实` 分层隔离后注入 LLM，而非碎片拼接。
- **事实隔离（防历史事实污染）**：方案层注入 `generalized_content`（去事实化正文，不注入原文）；证据层注入前做确定性脱敏（数字 / 机构名 / 日期 → 占位），历史标书里的「11 家 / 90 日历天 / 洛阳市总工会」等具体事实不会写进新标书。
- **方案组件（Solution Pattern）**：以章节为边界保存完整上下文 + 通用化 summary/structure + 去事实化 `generalized_content`。
- **知识节点**：每个 Pattern 的 source 扩到「命中 → 父 → 兄弟」章节。
- **方案组件抽取异步化**：原为串行调 LLM（154 章节实测 13.4 分钟，超过 Java 5 分钟超时，造成「Java 报失败 / AI 其实成功」）；现改为**主事务提交后异步抽取** + `BoundedExecutor` 有界并发（`PATTERN_EXTRACT_CONCURRENCY=4`，实测 256.5s，加速 3.14x）。
  代价：解析接口响应的 `pattern_count` **恒为 0**（属预期，不是 bug）；`code`（SP001…）按候选章节原始序号预分配，单节失败只跳过该节、编号留空洞。同一文档的并发抽取有进程内保护；**重新解析会先删旧 pattern 再写**（替换而非追加）。
- **租户隔离 + 索引守卫**：问答与检索按租户隔离（`app/core/tenant.py`）；`document_index_guard` 防并发/重复建索引。

## 脚本

| 脚本 | 用途 |
|------|------|
| `scripts/run_local_docx.py` | 本地解析 DOCX（走完整流水线） |
| `scripts/ingest_doc.py` | 解析 + 入库（document / chunk / pattern 全链路） |
| `scripts/extract_solution_patterns.py` | 离线抽取方案组件 |
| `scripts/regeneralize_patterns.py` | 迁移：回填已有 pattern 的 `generalized_content`（幂等） |
| `scripts/quarantine_tender_patterns.py` | 隔离误入的招标文件 pattern |
| `scripts/migrate_index_guard.py` | 迁移：建索引守卫表与索引 |
| `scripts/census_tenant_migration.py` | 租户数据普查 / 迁移 |
| `scripts/verify_p0_ingest.py` | 验证 P0 入库链路 |
| `scripts/verify_rag_v2.py` | 验证 RAG V2（意图 / 重排 / 事实隔离 / 知识节点 / 四层结构） |
| `scripts/verify_index_lifecycle.py` | 验证索引生命周期 |
| `scripts/verify_qa_doc_scope.py` | 验证问答的文档范围收敛 |
| `scripts/verify_tenant_isolation_dual.py` | 验证双租户隔离 |
| `scripts/test_rag_api.py` | 对 `POST /knowledge/rag` 跑多组 API 测试 |

```bash
uv run python scripts/verify_rag_v2.py           # RAG V2 端到端验证
uv run python scripts/test_rag_api.py            # API 测试（默认 http://localhost:8000）
uv run pytest tests/                             # 回归测试
```

## 当前进展

**代码规模**：`app/` 下 **89 个 Python 文件、7,668 行**；全仓（不含 `.venv`）**109 个 Python 文件**。本服务是三个子项目中完成度最高的一个。

| 阶段 | 能力 | 状态 |
| --- | --- | --- |
| Phase 1 | DOCX → AST → Section → Chunk → Embedding → pgvector 全链路 | ✅ |
| Phase 1 | 方案组件（Solution Pattern）抽取 + 去事实化 `generalized_content` | ✅ |
| Phase 2 | PDF 解析（`pdf_inspector` / PyMuPDF）；PaddleOCR / MinerU 降级接入 | ✅ |
| Phase 3 | 招标文件结构化分析：要求 / 评分点 / 风险（`/tender/analyze`） | ✅ |
| Phase 4 | RAG V2 检索链路（Query 理解 + 双通道召回 + reranker + 四层 Evidence Pack + 事实隔离） | ✅ |
| Phase 4 | RAG 生成（`/knowledge/rag`、`/knowledge/generate` 返回成文 `answer`） | ✅ |
| Phase 5 | 企业知识问答流式接口（`/knowledge/qa-stream`，SSE） | ✅ |
| Phase 5 | 租户隔离 + 索引守卫 + 方案组件抽取异步化 | ✅ |
| Phase 6 | 标书全文编排 / Word 导出 / AI 审核 | ⬜ 未开始 |

### 🔗 与平台其他端的集成状态

**已打通**。Java 侧 `bidox-service` 已通过 HTTP 调用本服务：

```
前端 bidox-ui (5666)
  └─ POST /admin-api/bid/knowledge-qa/chat-stream
       └─ bidox-service BidKnowledgeQaController（SseEmitter）
            └─ BidoxAiStreamingClient（Apache HttpClient 5）
                 └─ 本服务 POST /api/bidox/knowledge/qa-stream  ← 逐帧转发回前端
```

已接入的调用：`document/parse`（触发解析）、`document/delete`（清索引）、`knowledge/qa-stream`（问答流）、`retrieval/search`、`knowledge/generate`、`tender/analyze`。

## 已知问题

| 问题 | 影响 | 说明 |
| --- | --- | --- |
| **`uvicorn` 未开 `--reload`** | 改动不生效 | 改 Python 代码或 `.env` 后必须重启进程 |
| **未建向量索引** | 数据量大时检索变慢 | 仅 `cosine_distance` 顺序扫描，无 ivfflat/hnsw |
| **无 CORS 中间件** | 无 | 前端一律经 `bidox-service` 代理访问，不需要直连本服务；若将来要直连需自行加 `CORSMiddleware` |
| **Reranker 默认关闭** | 检索精度略降 | `RERANKER_ENABLED=false`：CPU 上重排 120 个候选约 260s，会超过 Java 侧 45s 的 idle 超时，前端表现为 `UPSTREAM_FAILED` |
| **`pattern_count` 恒为 0** | 无 | 方案组件抽取已异步化，响应不再等待抽取完成（见「关键设计」） |
| **Reranker 模型走 HF repo id 时首载慢** | 可能触发上游 idle 超时 | 离线环境建议把 `RERANKER_MODEL` 填成本地模型目录绝对路径 |
| **`app/main.py` 无 lifespan** | 无 | DB 引擎懒加载，无需启动钩子 |

## 相关项目

| 项目 | 路径 | 职责 | 端口 |
| --- | --- | --- | --- |
| **bixox-ai**（bidox-ai） | `ai-service/bixox-ai` | AI 服务：解析 / 向量化 / RAG / 生成（本仓库） | 8000 |
| **bidox-service** | `backend/bidox-service` | Web 端管理后台服务（Java / Spring Boot） | 48080 |
| **bidox-ui** | `frontend/bidox-ui` | 前端管理后台（Vben Admin v5.7.0） | 5666 |

## 开源协议

本仓库为 **专有软件**（Proprietary），详见 [LICENSE](./LICENSE)。第三方依赖与预训练模型各自遵循其原始许可证。
