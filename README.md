# BidOx AI 标书生成助手

BidOx 的 AI 服务(独立 Python 服务),负责**文档智能解析 → 结构化 → 向量化 → 检索 → AI 理解/生成**。

与 Java 侧(`bidox-service`)的边界:Java 负责用户/租户/企业/项目/文件上传/业务库/MQ 投递;Python 负责解析、AST、Section、Chunk、Embedding、pgvector 检索、RAG、LLM。

## 架构总览

```
文件(上传,由 Java 投递 MQ)
   │
   ▼
文件检测 / 类型识别 / MD5 去重
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
   ▼
Chunk (标题层级 + 语义 + 表格完整性综合切分)
   │
   ├── Metadata → PostgreSQL
   └── Embedding → pgvector
   │
   ▼
Hybrid Retrieval (向量 + 关键词 + Reranker)
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
│   ├── embedding/         # BGE-M3 向量化
│   ├── retrieval/         # 向量/关键词/混合检索 + reranker
│   ├── knowledge/         # 知识库索引与查询
│   ├── tender/            # 招标要求/评分/风险结构化提取
│   ├── llm/               # LLM 网关(deepseek/qwen/openai)
│   ├── tasks/             # MQ 任务消费者
│   └── api/               # FastAPI 路由
├── tests/
├── docker-compose.yml
└── pyproject.toml
```

## 快速开始

> 依赖用 [uv](https://docs.astral.sh/uv/) 管理。安装 uv:`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`(或 `pip install uv`)。

```bash
# 1. 启动基础设施
docker compose up -d

# 2. 安装依赖(核心 + 开发依赖),自动创建 .venv
uv sync

# 可选重型依赖(向量化 / OCR / MinerU / reranker)
uv sync --extra embedding --extra ocr

# 3. 配置
cp .env.example .env

# 4. 启动 API
uv run uvicorn app.main:app --reload --port 8000

# 5. 启动 MQ 消费者
uv run python -m app.tasks.consumer --queue parse
```

## 开发阶段(建议)

- **Phase 1**:DOCX → AST → Section → Chunk → Embedding → pgvector(先拿历史标书跑通)
- **Phase 2**:PDF → pdf_inspector,再加 MinerU / PaddleOCR
- **Phase 3**:招标 PDF → LLM 提取要求/评分/风险
- **Phase 4**:RAG + 标书生成 + AI 审核 + Word 导出

## 关键设计

- **统一 Document AST**:所有解析器(PDF/Word/OCR/MinerU)都输出同一种 AST,后面能力全部基于 AST。
- **表格完整性**:表格作为一个完整 Chunk,不拆散,保证评分项与其分值/标准不割裂。
- **保留 page**:chunk/section 记录 `page_start/page_end`,支撑"生成依据"可追溯。
- **懒加载重依赖**:OCR/MinerU/Embedding 未安装时降级为 stub 并告警,不阻塞主链路。
