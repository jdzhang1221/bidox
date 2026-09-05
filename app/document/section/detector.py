"""章节识别:根据标题层级把 Document AST 组织成 Section 树。"""

from __future__ import annotations

from app.core.logging import get_logger
from app.document.ast.models import Document, Heading
from app.document.section.models import Section

logger = get_logger(__name__)


class SectionDetector:
    """标题层级 -> 章节树。"""

    def detect(self, document: Document) -> list[Section]:
        """返回顶层 Section 列表(每节内含子章节)。"""
        roots: list[Section] = []
        # 栈保存 (level, section),用于挂载
        stack: list[tuple[int, Section]] = []
        # 无标题内容临时挂到最近的 section;完全无标题时挂到虚拟根
        pending_blocks: list = []

        for block in document.blocks:
            if isinstance(block, Heading):
                section = self._create_section(block)
                self._attach(section, stack, roots)
                # 将之前 pending 的块挂到新章节(如果栈为空,则作为根前导内容)
                if pending_blocks:
                    section.content_blocks = pending_blocks + section.content_blocks
                    pending_blocks = []
            else:
                if stack:
                    stack[-1][1].content_blocks.append(block)
                else:
                    pending_blocks.append(block)

        # 计算页码范围
        for section in roots:
            self._fill_page_range(section)
        return roots

    # --- 内部 ---
    @staticmethod
    def _create_section(heading: Heading) -> Section:
        return Section(
            title=heading.text,
            level=heading.level,
            section_no=heading.section_no,
            page_start=heading.page,
        )

    @staticmethod
    def _attach(section: Section, stack: list[tuple[int, Section]], roots: list[Section]) -> None:
        """根据层级弹出栈,挂到正确父节点。"""
        while stack and stack[-1][0] >= section.level:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            section.parent_id = None  # 由入库时回填真实 id
            parent.children.append(section)
        else:
            roots.append(section)
        stack.append((section.level, section))

    def _fill_page_range(self, section: Section) -> None:
        pages = [section.page_start]
        for child in section.children:
            self._fill_page_range(child)
            if child.page_start is not None:
                pages.append(child.page_start)
        section.page_end = max((p for p in pages if p is not None), default=None)
