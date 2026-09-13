"""方案组件(Solution Pattern)抽取原型脚本。

从历史标书里把「方案型章节」抽象成可复用的结构化方案组件,输出 JSON 供人工验证。

用法:
    uv run python scripts/extract_solution_patterns.py "<标书路径>" [--list-candidates]
        [--min-level 2] [--max-level 3] [--min-chars 300]
        [--output patterns.json] [--no-relations] [--no-cache]

说明:
    - 解析复用 DocumentPipeline,纯内存、无数据库依赖。
    - --list-candidates:离线模式,不调 LLM,只打印候选章节,先验证「以章节为边界」的粒度。
    - 完整抽取需配置 llm_api_key(.env),未配置会清晰报错退出。
    - 章节抽取结果默认缓存到 data/pattern_cache.json,重跑只补缺失章节(LLM 调用很贵,
      别因为最后一步失败就全丢)。
    - 落盘顺序:先写 patterns,再抽 relations 后覆写。关系抽取失败只影响 relations 字段。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK,强制 UTF-8 输出避免中文乱码(需终端为 UTF-8,否则先 `chcp 65001`)。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.document.pipeline import DocumentPipeline
from app.document.section.models import Section
from app.pattern.extractor import SolutionPatternExtractor
from app.pattern.models import SolutionPattern

logger = get_logger(__name__)

DEFAULT_CACHE = Path("data/pattern_cache.json")


def _run_pipeline(path: Path, document_type: str):
    return DocumentPipeline().run(path, document_type=document_type)


# --- 缓存:按「文档 + 章节 + 正文长度」定位,避免解析逻辑变化后误用旧结果 ---
def _cache_key(doc_title: str, section: Section) -> str:
    raw = f"{doc_title}|{section.section_no}|{section.title}|{len(section.full_content)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("缓存读取失败,忽略(%s): %s", path, exc)
        return {}


def _save_cache(path: Path, cache: dict[str, Any]) -> None:
    """原子写:先写临时文件再替换,避免中途崩溃留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _print_candidates(candidates) -> None:
    print(f"\n共 {len(candidates)} 个候选方案章节(level | section_type | 标题 | 全文字符数):\n")
    for s in candidates:
        chars = len(s.full_content.strip())
        print(f"  L{s.level} | {s.section_type or '-'} | {s.title} | {chars} 字")


def _print_summary(patterns, relations, cached_count) -> None:
    print(f"\n共抽取 {len(patterns)} 个方案组件(缓存命中 {cached_count})、{len(relations)} 条关系:\n")
    for p in patterns:
        print(
            f"  {p.code} | {p.type} | {p.name}"
            f" | 子模块 {len(p.structure)} | {len(p.content)} 字"
        )
    if relations:
        print("\n-- 组件关系 --")
        for r in relations:
            print(f"  {r.source} -> {r.target} ({r.rel_type})")


def _to_output(result, patterns, relations, candidates) -> dict:
    return {
        "document": {
            "title": result.document.title,
            "file_type": result.document.file_type,
            "source": result.document.source,
            "section_count": len(result.flattened_sections),
            "candidate_count": len(candidates),
        },
        "patterns": [p.to_dict() for p in patterns],
        "relations": [r.model_dump() for r in relations],
    }


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="方案组件(Solution Pattern)抽取原型")
    parser.add_argument("path", help="本地标书文件路径")
    parser.add_argument("--document-type", default="historical_bid", help="tender / historical_bid")
    parser.add_argument("--min-level", type=int, default=2, help="候选章节最小层级(默认 2)")
    parser.add_argument("--max-level", type=int, default=3, help="候选章节最大层级(默认 3)")
    parser.add_argument("--min-chars", type=int, default=300, help="候选章节全文字符数下限(默认 300)")
    parser.add_argument("--max-prompt-chars", type=int, default=6000, help="发给 LLM 的正文上限(默认 6000)")
    parser.add_argument("--limit", type=int, default=0, help="只抽取前 N 个候选(默认 0=全部),用于冒烟测试")
    parser.add_argument("--output", help="输出 JSON 文件路径(默认打印到 stdout)")
    parser.add_argument("--no-relations", action="store_true", help="跳过组件关系抽取")
    parser.add_argument("--no-filter", action="store_true", help="不过滤非方案章节(保留全部候选)")
    parser.add_argument("--list-candidates", action="store_true", help="离线模式:只列候选章节,不调 LLM")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help=f"章节抽取缓存路径(默认 {DEFAULT_CACHE})")
    parser.add_argument("--no-cache", action="store_true", help="禁用缓存,全部重新调 LLM")
    parser.add_argument("--extract-timeout", type=float, default=180.0, help="单章节抽取超时秒(默认 180)")
    parser.add_argument("--relations-timeout", type=float, default=300.0, help="关系抽取超时秒(默认 300)")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"文件不存在: {args.path}")

    result = _run_pipeline(path, args.document_type)

    candidates = SolutionPatternExtractor.select_candidates(
        result.sections,
        min_level=args.min_level,
        max_level=args.max_level,
        min_chars=args.min_chars,
        filter_non_solution=not args.no_filter,
    )
    if args.limit > 0:
        candidates = candidates[: args.limit]

    if args.list_candidates:
        _print_candidates(candidates)
        return

    cache_path = Path(args.cache)
    cache = {} if args.no_cache else _load_cache(cache_path)
    doc_title = result.document.title
    doc_meta = {"title": doc_title, "file_type": result.document.file_type}

    # LLM 网关懒初始化:全部命中缓存时无需 api_key,也能离线复现结果。
    extractor_box: dict[str, SolutionPatternExtractor] = {}

    def _extractor() -> SolutionPatternExtractor:
        if "llm" not in extractor_box:
            if not settings.llm_api_key:
                raise SystemExit(
                    "未配置 llm_api_key(.env 中 LLM_API_KEY 为空)。"
                    "请先配置后再跑完整抽取,或先用 --list-candidates 离线验证候选章节。"
                )
            extractor_box["llm"] = SolutionPatternExtractor()
        return extractor_box["llm"]

    patterns: list[SolutionPattern] = []
    errors: list[tuple[str, str, str]] = []
    cached_count = 0
    for i, sec in enumerate(candidates, 1):
        code = f"SP{i:03d}"
        key = _cache_key(doc_title, sec)
        hit = cache.get(key)
        if isinstance(hit, dict):
            try:
                patterns.append(SolutionPattern(**{**hit, "code": code}))
                cached_count += 1
                continue
            except Exception as exc:  # 缓存损坏则重抽,不影响主流程
                logger.warning("缓存项不可用 %s %s: %s", code, sec.title, exc)

        try:
            pattern = _extractor().extract(
                sec,
                code=code,
                doc_meta=doc_meta,
                max_prompt_chars=args.max_prompt_chars,
                timeout=args.extract_timeout,
            )
            patterns.append(pattern)
            if not args.no_cache:
                cache[key] = pattern.to_dict()
                _save_cache(cache_path, cache)  # 每节即存,崩溃也不丢已完成部分
        except Exception as exc:  # pragma: no cover - LLM 单节失败不影响其余
            logger.warning("抽取失败 %s %s: %s", code, sec.title, exc)
            errors.append((code, sec.title, str(exc)))

    # 先落盘 patterns:关系抽取是增强步骤,不该有能力让已抽取结果消失。
    output = _to_output(result, patterns, [], candidates)
    if args.output:
        _write_json(Path(args.output), output)
        print(f"已写入 {args.output}({len(patterns)} 个组件,关系抽取中…)")

    relations = []
    if not args.no_relations and len(patterns) >= 2:
        try:
            relations = _extractor().extract_relations(
                patterns, timeout=args.relations_timeout
            )
        except Exception as exc:  # pragma: no cover - 关系失败降级为空
            logger.warning("关系抽取整体失败,输出 relations=[]: %s", exc)

    output = _to_output(result, patterns, relations, candidates)
    if args.output:
        _write_json(Path(args.output), output)
        print(f"已写入 {args.output}")
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))

    _print_summary(patterns, relations, cached_count)
    if errors:
        print(f"\n[警告] {len(errors)} 个章节抽取失败:")
        for code, title, err in errors:
            print(f"  {code} {title}: {err}")


if __name__ == "__main__":
    main()
