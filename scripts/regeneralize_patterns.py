"""把已有 solution_pattern 的 content 去事实化,回填 generalized_content 列。

用法:
  uv run python scripts/regeneralize_patterns.py           # 只处理 generalized_content 为空的
  uv run python scripts/regeneralize_patterns.py --force    # 全部重跑(幂等覆盖)
"""

from __future__ import annotations

import argparse

from sqlalchemy import select, text, update

from app.core.database import session_scope
from app.core.logging import get_logger
from app.llm.gateway import get_llm
from app.models.pattern import SolutionPattern

logger = get_logger(__name__)

_SYSTEM = (
    "你是标书方案分析专家。请把给定历史标书章节正文改写为「去事实化正文」:"
    "保留完整方法与行文结构,但把具体数字、单位、人名、机构名、项目名、日期等一律替换为通用表述"
    "(如「11家直属基层工会」→「被审计单位」,「90日历天」→「合同约定期限内」)。"
    "只输出改写后的正文,不要任何解释或 Markdown 标记。"
)
_MAX_PROMPT_CHARS = 8000


def _ensure_column() -> None:
    with session_scope() as session:
        session.execute(
            text("ALTER TABLE solution_pattern ADD COLUMN IF NOT EXISTS generalized_content TEXT")
        )
    logger.info("generalized_content 列已就绪")


def _load_pending(force: bool) -> list[tuple[int, str | None, str | None]]:
    with session_scope() as session:
        stmt = select(
            SolutionPattern.id, SolutionPattern.code, SolutionPattern.content,
            SolutionPattern.generalized_content,
        )
        if not force:
            stmt = stmt.where(SolutionPattern.generalized_content.is_(None))
        rows = session.execute(stmt).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _update(pid: int, text: str) -> None:
    with session_scope() as session:
        session.execute(
            update(SolutionPattern)
            .where(SolutionPattern.id == pid)
            .values(generalized_content=text)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="覆盖已有 generalized_content 重跑")
    args = parser.parse_args()

    _ensure_column()
    rows = _load_pending(args.force)
    if not rows:
        logger.info("无待处理 pattern")
        return

    llm = get_llm()
    ok = skip = fail = 0
    for pid, code, content, _existing in rows:
        if not content or not content.strip():
            logger.warning("[%s] content 为空,跳过", code)
            skip += 1
            continue
        try:
            generalized = llm.complete(
                f"历史标书章节正文:\n{content[:_MAX_PROMPT_CHARS]}",
                system=_SYSTEM,
                timeout=180,
            ).strip()
        except Exception as exc:  # LLM 超时/限流:跳过该条,不中断
            logger.warning("[%s] 去事实化失败: %s", code, exc)
            fail += 1
            continue
        if not generalized:
            logger.warning("[%s] 返回为空,跳过", code)
            skip += 1
            continue
        _update(pid, generalized)
        ok += 1
        logger.info("[%s] 已回填 generalized_content (%d 字)", code, len(generalized))

    logger.info("完成: ok=%d skip=%d fail=%d", ok, skip, fail)


if __name__ == "__main__":
    main()
