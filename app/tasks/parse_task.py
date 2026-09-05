"""解析任务处理:拉取文件 -> 解析 -> Section -> Chunk。"""

from __future__ import annotations

from app.core.logging import get_logger
from app.core.storage import get_storage
from app.document.pipeline import DocumentPipeline, ParseResult
from app.tasks.schemas import ParseTaskMessage

logger = get_logger(__name__)


def handle_parse_task(message: ParseTaskMessage) -> ParseResult:
    """处理一个解析任务。

    流程:
      1. 根据 file_url 拉取文件到本地
      2. 执行 DocumentPipeline(解析/AST/Section/Chunk)
      3. 返回 ParseResult(调用方决定是否落库/向量化)
    """
    logger.info("处理解析任务: task=%s document=%s", message.task_id, message.document_id)

    storage = get_storage()
    local_path = storage.download(message.file_url)

    pipeline = DocumentPipeline(document_id=message.document_id)
    result = pipeline.run(local_path, document_type=message.document_type)
    return result
