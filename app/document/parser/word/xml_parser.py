"""DOCX 底层 XML 增强解析。

python-docx 对复杂样式/标题编号/列表/页眉页脚支持有限,这里直接读
word/document.xml / word/numbering.xml 补充标题级别与列表结构。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

# Word 样式名 -> 标题级别启发式
_HEADING_STYLE = re.compile(r"(?i)^(heading|标题)\s*([1-9])")
# 中文标题编号:第X章 / X.X.X / 一、二、
_SECTION_NO = re.compile(r"^(第[一二三四五六七八九十百0-9]+[章节篇])|^(\d+(?:\.\d+)*)")


def parse_document_xml(path: Path) -> dict:
    """读取 docx 内部 XML 关键信息,返回增强字典。

    返回:
        {"headings": {para_index: level}, "numbering": {...}}
    供 docx_parser 校正标题级别与列表。
    """
    result: dict = {"headings": {}, "list_paras": set()}
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "word/document.xml" not in names:
                return result
            document_xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:
        return result

    # 提取 pStyle 与 numPr,关联段落索引
    from xml.etree import ElementTree as ET

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError:
        return result

    for idx, para in enumerate(root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")):
        # 标题样式
        pstyle = para.find(".//w:pStyle", ns)
        if pstyle is not None:
            val = pstyle.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "")
            m = _HEADING_STYLE.match(val)
            if m:
                result["headings"][idx] = int(m.group(2))
        # 列表项
        numpr = para.find(".//w:numPr", ns)
        if numpr is not None:
            result["list_paras"].add(idx)
    return result


def extract_section_no(text: str) -> str | None:
    """从标题文本提取章节编号,如 '5.1.2 数据架构' -> '5.1.2'。"""
    m = _SECTION_NO.match(text.strip())
    if m:
        return m.group(1) or m.group(2)
    return None


def guess_heading_level(text: str) -> int | None:
    """纯文本启发式标题级别(无样式信息时用)。"""
    t = text.strip()
    if re.match(r"^第[一二三四五六七八九十百0-9]+[章节篇]", t):
        return 1
    if re.match(r"^[一二三四五六七八九十]+、", t):
        return 2
    if re.match(r"^\d+(\.\d+){0,2}\s+\S", t):
        dots = t.split(" ")[0].count(".")
        return min(dots + 1, 4)
    return None
