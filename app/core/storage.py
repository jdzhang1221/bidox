"""文件存储抽象(MinIO / OSS)。

Python 侧不负责上传(上传由 Java 完成),这里只需要根据 `storage_path` 拉取文件到本地供解析。
未配置 MinIO 时,本地路径直接透传,便于开发调试。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class StorageClient:
    """统一存储客户端接口。"""

    def download(self, storage_path: str, local_dir: str | None = None) -> Path:
        """下载对象到本地临时文件,返回本地路径。"""
        raise NotImplementedError


class MinIOClient(StorageClient):
    """MinIO 实现(懒加载 minio 包)。"""

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from minio import Minio
            except ImportError as exc:  # pragma: no cover - 依赖未装
                raise RuntimeError("未安装 minio 包: pip install minio") from exc
            self._client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
        return self._client

    def download(self, storage_path: str, local_dir: str | None = None) -> Path:
        client = self._get_client()
        bucket, _, object_name = storage_path.partition("/")
        # 兼容 minio://bucket/object 前缀
        if bucket.startswith(("minio://", "oss://", "s3://")):
            _, _, bucket = bucket.partition("://")
        suffix = Path(object_name).suffix
        dest = Path(local_dir or tempfile.gettempdir()) / (Path(object_name).stem + suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.fget_object(bucket, object_name, str(dest))
        logger.info("已下载 %s/%s -> %s", bucket, object_name, dest)
        return dest


class LocalClient(StorageClient):
    """本地文件透传(开发调试用)。"""

    def download(self, storage_path: str, local_dir: str | None = None) -> Path:
        path = Path(storage_path)
        if not path.exists():
            raise FileNotFoundError(f"本地文件不存在: {storage_path}")
        return path


def get_storage() -> StorageClient:
    """根据配置返回存储客户端。"""
    if settings.minio_endpoint and settings.minio_access_key:
        return MinIOClient()
    return LocalClient()
