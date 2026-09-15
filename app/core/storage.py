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
    """本地文件透传(开发调试用)。

    Java 侧本地存储传过来的 `storage_path` 通常是**相对路径**
    (如 `bid-document/20260914/xxx.docx`),需要基于 `local_storage_base_dir` 拼成绝对路径;
    若已是绝对路径则直接使用。同时兼容 http(s) 直链(如 Java 的 `/infra/file/{id}/get/...`)。
    """

    def download(self, storage_path: str, local_dir: str | None = None) -> Path:
        # http(s) 直链:下载到本地临时文件
        if storage_path.startswith(("http://", "https://")):
            return self._download_http(storage_path, local_dir)

        path = Path(storage_path)
        if not path.is_absolute():
            base = settings.local_storage_base_dir
            if base:
                path = Path(base) / path
        if not path.exists():
            raise FileNotFoundError(
                f"本地文件不存在: {storage_path} (解析为 {path})"
            )
        logger.info("使用本地文件 %s", path)
        return path

    @staticmethod
    def _download_http(url: str, local_dir: str | None = None) -> Path:
        """从 http(s) 直链下载到本地临时文件。"""
        import urllib.request
        from urllib.parse import unquote, urlparse

        name = Path(unquote(urlparse(url).path)).name or "download"
        dest = Path(local_dir or tempfile.gettempdir()) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
            dest.write_bytes(resp.read())
        logger.info("已下载 %s -> %s", url, dest)
        return dest


def get_storage(provider: str | None = None) -> StorageClient:
    """根据配置 / 请求指定的存储提供者返回存储客户端。

    - provider="local":强制本地文件系统(一期 Java 侧固定传 local)
    - provider in ("minio","oss","s3"):强制对象存储
    - provider 为空:按 settings.storage_provider,再回退到「配了 MinIO 就用 MinIO」
    """
    provider = (provider or settings.storage_provider or "").strip().lower()
    if provider == "local":
        return LocalClient()
    if provider in ("minio", "oss", "s3"):
        return MinIOClient()
    if settings.minio_endpoint and settings.minio_access_key:
        return MinIOClient()
    return LocalClient()
