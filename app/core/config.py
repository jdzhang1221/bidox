"""应用配置。

基于 pydantic-settings,从环境变量 / `.env` 读取。所有配置项见 `.env.example`。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """BidOx AI 全局配置。"""

    model_config = SettingsConfigDict(
        env_file=_BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 服务 ---
    app_name: str = "BidOx AI"
    app_env: str = "dev"
    debug: bool = True
    api_prefix: str = "/api/bidox"

    # --- PostgreSQL + pgvector ---
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/bidox_ai"
    vector_dim: int = 1024

    # --- RabbitMQ ---
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    queue_parse: str = "bidox.document.parse"
    queue_embedding: str = "bidox.document.embedding"
    queue_analysis: str = "bidox.tender.analysis"

    # --- MinIO / OSS ---
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "bidox"
    minio_secure: bool = False

    # --- 本地文件存储 ---
    # Java 侧本地存储(LocalFileClient)的根目录。Java 传给 AI 的 storageKey 是相对路径
    # (如 bid-document/20260914/xxx.docx),AI 需要用它拼出真实绝对路径。
    local_storage_base_dir: str = ""
    # 默认存储提供者:local / minio。请求未显式指定 storageProvider 时生效。
    storage_provider: str = "local"

    # --- Embedding ---
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    embedding_batch_size: int = 32
    embedding_required: bool = False
    # local = FlagEmbedding 进程内加载; ollama = 走 Ollama HTTP API
    embedding_provider: str = "local"

    # --- Ollama (embedding_provider=ollama 时使用) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "bge-m3"
    ollama_timeout: float = 60.0

    # --- Reranker ---
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_top_k: int = 5
    reranker_enabled: bool = False
    reranker_recall_k: int = 20  # 重排前召回候选数(recall-then-rerank)

    # --- LLM ---
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.1
    llm_timeout: float = 120.0
    # 方案组件抽取的并发度(逐章节调 LLM,IO 密集)。串行跑 150+ 章节标书需 10 分钟以上,
    # 并发后总耗时约降为 1/N。取值受 provider 限流约束,DeepSeek 建议 4~8。
    pattern_extract_concurrency: int = 4

    # --- 切分 ---
    chunk_max_chars: int = 1200
    chunk_overlap: int = 150
    chunk_min_chars: int = 80

    # --- 检索 ---
    retrieval_top_k: int = 10
    retrieval_fusion_k: int = 60

    # --- 企业事实(enterprise_facts 层:document_type 定向检索) ---
    enterprise_fact_document_types: list[str] = ["enterprise_profile", "qualification", "project_case"]
    enterprise_fact_top_k: int = 5

    @property
    def minio_storage_url(self) -> str:
        scheme = "https" if self.minio_secure else "http"
        return f"{scheme}://{self.minio_endpoint}"


@lru_cache
def get_settings() -> Settings:
    """获取全局配置(单例)。"""
    return Settings()


settings = get_settings()
