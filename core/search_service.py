from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from .rule_engine import RuleEngine, RuleEngineError
from .source_registry import SourceRegistry


@dataclass
class SearchServiceConfig:
    max_workers: int = 4
    time_budget_seconds: float = 45.0


class SearchService:
    def __init__(
        self,
        registry: SourceRegistry,
        engine: RuleEngine,
        config: Optional[SearchServiceConfig] = None,
    ):
        self.registry = registry
        self.engine = engine
        self.config = config or SearchServiceConfig()

    def search(
        self,
        keyword: str,
        source_ids: Optional[Iterable[str]] = None,
        limit: int = 20,
        include_disabled: bool = False,
    ) -> Dict[str, Any]:
        normalized_keyword = str(keyword or "").strip()
        if not normalized_keyword:
            raise ValueError("搜索关键词不能为空")

        source_summaries = self.registry.load_enabled_source_summaries(
            source_ids=source_ids,
            include_disabled=include_disabled,
        )
        if not source_summaries:
            return {
                "keyword": normalized_keyword,
                "searched_sources": 0,
                "skipped_sources": [],
                "successful_sources": 0,
                "result_count": 0,
                "results": [],
                "errors": [],
            }

        searchable_summaries = [item for item in source_summaries if item.get("supports_search")]
        skipped_sources = [
            {
                "source_id": item.get("source_id", ""),
                "source_name": item.get("name", ""),
                "reason": "；".join(item.get("issues") or []) or "当前书源不支持 route A 搜索",
            }
            for item in source_summaries
            if not item.get("supports_search")
        ]
        if not searchable_summaries:
            return {
                "keyword": normalized_keyword,
                "searched_sources": 0,
                "skipped_sources": skipped_sources,
                "successful_sources": 0,
                "result_count": 0,
                "results": [],
                "errors": [],
            }

        sources = self.registry.load_enabled_sources(
            source_ids=[item["source_id"] for item in searchable_summaries],
            include_disabled=include_disabled,
        )

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        max_workers = min(max(1, self.config.max_workers), len(sources))
        completed_sources = 0
        timed_out_sources = 0
        partial = False
        deadline = time.monotonic() + max(0.1, float(self.config.time_budget_seconds))

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        pending: set[concurrent.futures.Future] = set()
        try:
            future_map = {
                executor.submit(self.engine.search_books, source, normalized_keyword, limit): source
                for source in sources
            }
            pending = set(future_map)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    partial = True
                    timed_out_sources = len(pending)
                    break

                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=remaining,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    partial = True
                    timed_out_sources = len(pending)
                    break

                for future in done:
                    source = future_map[future]
                    completed_sources += 1
                    try:
                        source_results = future.result()
                    except RuleEngineError as exc:
                        errors.append(
                            {
                                "source_id": source.get("source_id", ""),
                                "source_name": source.get("name", ""),
                                "error": str(exc),
                            }
                        )
                        continue
                    except Exception as exc:
                        errors.append(
                            {
                                "source_id": source.get("source_id", ""),
                                "source_name": source.get("name", ""),
                                "error": "未预期错误: {error}".format(error=exc),
                            }
                        )
                        continue
                    results.extend(source_results)
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False)

        results.sort(key=lambda item: self._score_result(item, normalized_keyword))
        return {
            "keyword": normalized_keyword,
            "searched_sources": len(sources),
            "completed_sources": completed_sources,
            "skipped_sources": skipped_sources,
            "successful_sources": max(0, completed_sources - len(errors)),
            "partial": partial,
            "timed_out_source_count": timed_out_sources,
            "result_count": len(results),
            "results": results[: max(1, limit)],
            "errors": errors,
        }

    def _score_result(self, item: Dict[str, Any], keyword: str) -> tuple:
        title = str(item.get("title") or "").strip().lower()
        author = str(item.get("author") or "").strip().lower()
        normalized_keyword = keyword.lower()

        if title == normalized_keyword:
            return (0, title, author)
        if normalized_keyword in title:
            return (1, title, author)
        return (2, title, author)
