"""ORM 模型注册:document / document_section / document_chunk / knowledge_base / solution_pattern / pattern_source / index_guard。"""

from app.models.chunk import DocumentChunk
from app.models.document import DocumentRecord
from app.models.index_guard import DocumentIndexGuard, DocumentPatternJob
from app.models.knowledge_base import KnowledgeBase
from app.models.pattern import PatternSource, SolutionPattern
from app.models.section import DocumentSection

__all__ = [
    "DocumentChunk",
    "DocumentIndexGuard",
    "DocumentPatternJob",
    "DocumentRecord",
    "DocumentSection",
    "KnowledgeBase",
    "SolutionPattern",
    "PatternSource",
]
