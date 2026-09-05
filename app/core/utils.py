"""通用工具:文件类型检测、哈希、去重。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# 常见文件 magic number / 特征
_MAGIC = {
    "pdf": b"%PDF",
    "zip": b"PK\x03\x04",  # docx 本质是 zip
    "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # OLE2 复合文档
}

# 扩展名 -> 规范类型
_EXT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
    ".txt": "txt",
    ".md": "txt",
}


@dataclass
class FileMeta:
    """文件元信息。"""

    path: Path
    ext: str
    detected_type: str | None
    size: int
    md5: str
    sha256: str

    @property
    def is_pdf(self) -> bool:
        return self.detected_type == "pdf"

    @property
    def is_docx(self) -> bool:
        return self.detected_type == "docx"

    @property
    def is_doc(self) -> bool:
        return self.detected_type == "doc"


def detect_type(path: Path) -> str | None:
    """综合扩展名 + magic number 判断文件真实类型。

    不轻信扩展名:优先用文件头,扩展名作为回退。
    """
    ext = _EXT_MAP.get(path.suffix.lower())
    if not path.exists():
        return ext
    head = _read_head(path, 8)
    # magic 判断
    if head.startswith(_MAGIC["pdf"]):
        return "pdf"
    if head.startswith(_MAGIC["zip"]):
        # docx 是 zip,进一步检查是否含 word/document.xml 以排除普通 zip
        if _is_docx_zip(path):
            return "docx"
        return ext or "zip"
    if head.startswith(_MAGIC["doc"]):
        return "doc"
    # 纯文本
    if ext == "txt" and _looks_like_text(path):
        return "txt"
    return ext


def _is_docx_zip(path: Path) -> bool:
    """检查 zip 内是否含 word/document.xml(判定为 docx)。"""
    try:
        import zipfile

        with zipfile.ZipFile(path) as zf:
            return "word/document.xml" in zf.namelist()
    except Exception:
        return False


def _looks_like_text(path: Path) -> bool:
    try:
        _read_head(path, 1024).decode("utf-8")
        return True
    except Exception:
        return False


def _read_head(path: Path, n: int) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except Exception:
        return b""


def file_hashes(path: Path) -> tuple[str, str]:
    """计算 MD5 / SHA256。"""
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def inspect_file(path: str | Path) -> FileMeta:
    """生成文件元信息。"""
    p = Path(path)
    md5, sha = file_hashes(p)
    return FileMeta(
        path=p,
        ext=p.suffix.lower(),
        detected_type=detect_type(p),
        size=p.stat().st_size,
        md5=md5,
        sha256=sha,
    )
