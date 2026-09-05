"""RabbitMQ 消费者:按队列监听并分派到对应任务处理器。

用法:
    python -m app.tasks.consumer --queue parse
    python -m app.tasks.consumer --queue embedding
    python -m app.tasks.consumer --queue analysis
"""

from __future__ import annotations

import argparse
import json

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.tasks.analysis_task import handle_analysis_task
from app.tasks.embedding_task import handle_embedding_task
from app.tasks.parse_task import handle_parse_task
from app.tasks.schemas import AnalysisTaskMessage, EmbeddingTaskMessage, ParseTaskMessage

logger = get_logger(__name__)

_QUEUE_HANDLERS = {
    settings.queue_parse: ("parse", ParseTaskMessage, handle_parse_task),
    settings.queue_embedding: ("embedding", EmbeddingTaskMessage, handle_embedding_task),
    settings.queue_analysis: ("analysis", AnalysisTaskMessage, handle_analysis_task),
}


def _on_message(ch, method, properties, body, handler):
    try:
        payload = json.loads(body)
        message = handler[1].model_validate(payload)
        logger.info("收到任务: %s", handler[0])
        handler[2](message)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception:
        logger.exception("任务处理失败,进入 dead letter")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def consume(queue_key: str) -> None:
    """监听指定队列。"""
    try:
        import pika
    except ImportError as exc:
        raise SystemExit("未安装 pika: pip install pika") from exc

    handler = _QUEUE_HANDLERS[queue_key]

    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    channel = connection.channel()
    channel.queue_declare(queue=queue_key, durable=True)
    channel.basic_qos(prefetch_count=1)

    def callback(ch, method, properties, body):
        _on_message(ch, method, properties, body, handler)

    channel.basic_consume(queue=queue_key, on_message_callback=callback)
    logger.info("开始监听队列: %s", queue_key)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    finally:
        connection.close()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="BidOx MQ 消费者")
    parser.add_argument("--queue", required=True, choices=list(_QUEUE_HANDLERS.keys()),
                        help="要监听的队列")
    args = parser.parse_args()
    consume(args.queue)


if __name__ == "__main__":
    main()
