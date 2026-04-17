from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .rule_engine import RuleEngine, RuleEngineError
from .source_registry import SourceRegistry


@dataclass
class SearchServiceConfig:
    max_workers: int = 4
    time_budget_seconds: float = 45.0
    health_path: str | Path | None = None


class SearchService:
    _HEALTH_SCHEMA_VERSION = 1

    def __init__(
        self,
        registry: SourceRegistry,
        engine: RuleEngine,
        config: Optional[SearchServiceConfig] = None,
    ):
        self.registry = registry
        self.engine = engine
        self.config = config or SearchServiceConfig()
        self._health_lock = threading.Lock()
        health_path = self.config.health_path
        self._health_path = Path(health_path) if health_path else None
        self._source_health = self._load_source_health()

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
        summary_by_source_id = {
            str(item.get("source_id") or ""): item for item in searchable_summaries
        }
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
        sources = sorted(
            sources,
            key=lambda item: self._source_priority_key(item, summary_by_source_id),
        )

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        max_workers = min(max(1, self.config.max_workers), len(sources))
        dispatch_window = max_workers
        completed_sources = 0
        dispatched_sources = 0
        timed_out_sources = 0
        unsearched_sources = 0
        partial = False
        early_stopped = False
        stop_reason = ""
        deadline = time.monotonic() + max(0.1, float(self.config.time_budget_seconds))
        source_outcomes: list[dict[str, Any]] = []

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        pending: set[concurrent.futures.Future] = set()
        future_map: dict[concurrent.futures.Future, Dict[str, Any]] = {}
        started_at_map: dict[concurrent.futures.Future, float] = {}
        try:
            source_index = 0
            source_index, dispatched_sources = self._dispatch_search_tasks(
                executor,
                sources,
                source_index,
                dispatch_window,
                normalized_keyword,
                limit,
                pending,
                future_map,
                started_at_map,
                dispatched_sources,
            )
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    partial = True
                    stop_reason = "time_budget"
                    timed_out_sources = len(pending)
                    unsearched_sources = max(0, len(sources) - dispatched_sources)
                    source_outcomes.extend(
                        self._build_timed_out_outcomes(
                            pending,
                            future_map,
                            started_at_map,
                        )
                    )
                    break

                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=remaining,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    partial = True
                    stop_reason = "time_budget"
                    timed_out_sources = len(pending)
                    unsearched_sources = max(0, len(sources) - dispatched_sources)
                    source_outcomes.extend(
                        self._build_timed_out_outcomes(
                            pending,
                            future_map,
                            started_at_map,
                        )
                    )
                    break

                for future in done:
                    source = future_map[future]
                    started_at = started_at_map.pop(future, time.monotonic())
                    elapsed_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
                    completed_sources += 1
                    try:
                        source_results = future.result()
                        source_outcomes.append(
                            self._make_source_outcome(
                                source,
                                elapsed_ms,
                                success=True,
                            )
                        )
                    except RuleEngineError as exc:
                        error_text = str(exc)
                        errors.append(
                            {
                                "source_id": source.get("source_id", ""),
                                "source_name": source.get("name", ""),
                                "error": error_text,
                            }
                        )
                        source_outcomes.append(
                            self._make_source_outcome(
                                source,
                                elapsed_ms,
                                success=False,
                                timed_out=self._looks_like_timeout_error(error_text),
                            )
                        )
                        continue
                    except Exception as exc:
                        error_text = "未预期错误: {error}".format(error=exc)
                        errors.append(
                            {
                                "source_id": source.get("source_id", ""),
                                "source_name": source.get("name", ""),
                                "error": error_text,
                            }
                        )
                        source_outcomes.append(
                            self._make_source_outcome(
                                source,
                                elapsed_ms,
                                success=False,
                                timed_out=self._looks_like_timeout_error(error_text),
                            )
                        )
                        continue
                    results.extend(
                        self._attach_source_summary_fields(
                            source_results,
                            summary_by_source_id.get(str(source.get("source_id") or ""), {}),
                        )
                    )
                for future in done:
                    future_map.pop(future, None)

                if self._should_stop_early(results, normalized_keyword, limit):
                    partial = True
                    early_stopped = True
                    stop_reason = "exact_match_limit"
                    unsearched_sources = max(0, len(sources) - dispatched_sources)
                    break

                source_index, dispatched_sources = self._dispatch_search_tasks(
                    executor,
                    sources,
                    source_index,
                    dispatch_window,
                    normalized_keyword,
                    limit,
                    pending,
                    future_map,
                    started_at_map,
                    dispatched_sources,
                )
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False)

        self._record_source_outcomes(source_outcomes)

        results.sort(key=lambda item: self._score_result(item, normalized_keyword))
        return {
            "keyword": normalized_keyword,
            "candidate_sources": len(sources),
            "searched_sources": dispatched_sources,
            "completed_sources": completed_sources,
            "skipped_sources": skipped_sources,
            "successful_sources": max(0, completed_sources - len(errors)),
            "partial": partial,
            "early_stopped": early_stopped,
            "stop_reason": stop_reason,
            "timed_out_source_count": timed_out_sources,
            "unsearched_source_count": unsearched_sources,
            "result_count": len(results),
            "results": results[: max(1, limit)],
            "errors": errors,
        }

    def _score_result(self, item: Dict[str, Any], keyword: str) -> tuple:
        title = str(item.get("title") or "").strip().lower()
        author = str(item.get("author") or "").strip().lower()
        normalized_keyword = keyword.lower()
        download_rank = 0 if item.get("supports_download") else 1

        if title == normalized_keyword:
            return (0, download_rank, title, author)
        if normalized_keyword in title:
            return (1, download_rank, title, author)
        return (2, download_rank, title, author)

    def _attach_source_summary_fields(
        self,
        items: List[Dict[str, Any]],
        summary: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        enriched: List[Dict[str, Any]] = []
        supports_download = bool(summary.get("supports_download", False))
        issues = [str(issue).strip() for issue in list(summary.get("issues") or []) if str(issue).strip()]
        for item in items:
            current = dict(item)
            current["supports_download"] = supports_download
            if issues:
                current["source_issues"] = issues[:3]
            enriched.append(current)
        return enriched

    def _dispatch_search_tasks(
        self,
        executor: concurrent.futures.ThreadPoolExecutor,
        sources: list[Dict[str, Any]],
        source_index: int,
        dispatch_window: int,
        keyword: str,
        limit: int,
        pending: set[concurrent.futures.Future],
        future_map: dict[concurrent.futures.Future, Dict[str, Any]],
        started_at_map: dict[concurrent.futures.Future, float],
        dispatched_sources: int,
    ) -> tuple[int, int]:
        while len(pending) < dispatch_window and source_index < len(sources):
            source = sources[source_index]
            source_index += 1
            future = executor.submit(self.engine.search_books, source, keyword, limit)
            future_map[future] = source
            started_at_map[future] = time.monotonic()
            pending.add(future)
            dispatched_sources += 1
        return source_index, dispatched_sources

    def _should_stop_early(self, results: list[Dict[str, Any]], keyword: str, limit: int) -> bool:
        normalized_keyword = keyword.lower()
        exact_match_count = 0
        for item in results:
            title = str(item.get("title") or "").strip().lower()
            if title != normalized_keyword:
                continue
            exact_match_count += 1
            if exact_match_count >= max(1, limit):
                return True
        return False

    def _looks_like_timeout_error(self, message: str) -> bool:
        text = str(message or "").lower()
        return "timeout" in text or "timed out" in text or "超时" in text

    def _build_timed_out_outcomes(
        self,
        pending: set[concurrent.futures.Future],
        future_map: dict[concurrent.futures.Future, Dict[str, Any]],
        started_at_map: dict[concurrent.futures.Future, float],
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        now = time.monotonic()
        for future in pending:
            source = future_map.get(future)
            if source is None:
                continue
            started_at = started_at_map.get(future, now)
            outcomes.append(
                self._make_source_outcome(
                    source,
                    max(0.0, (now - started_at) * 1000.0),
                    success=False,
                    timed_out=True,
                )
            )
        return outcomes

    def _make_source_outcome(
        self,
        source: Dict[str, Any],
        elapsed_ms: float,
        success: bool,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        return {
            "source_id": str(source.get("source_id") or ""),
            "elapsed_ms": float(elapsed_ms),
            "success": bool(success),
            "timed_out": bool(timed_out),
            "recorded_at": time.time(),
        }

    def _record_source_outcomes(self, outcomes: list[dict[str, Any]]) -> None:
        if not outcomes:
            return
        with self._health_lock:
            for item in outcomes:
                self._apply_source_outcome(item)
            self._save_source_health_locked()

    def _apply_source_outcome(self, outcome: dict[str, Any]) -> None:
        source_id = str(outcome.get("source_id") or "").strip()
        if not source_id:
            return
        entry = self._source_health.setdefault(
            source_id,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "timeouts": 0,
                "avg_duration_ms": 0.0,
                "avg_success_ms": 0.0,
                "last_success_at": 0.0,
                "last_failure_at": 0.0,
            },
        )
        elapsed_ms = max(0.0, float(outcome.get("elapsed_ms", 0.0) or 0.0))
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["avg_duration_ms"] = self._rolling_average(
            float(entry.get("avg_duration_ms", 0.0) or 0.0),
            elapsed_ms,
            int(entry["attempts"]),
        )
        recorded_at = float(outcome.get("recorded_at", time.time()) or time.time())
        if outcome.get("success"):
            entry["successes"] = int(entry.get("successes", 0)) + 1
            entry["avg_success_ms"] = self._rolling_average(
                float(entry.get("avg_success_ms", 0.0) or 0.0),
                elapsed_ms,
                int(entry["successes"]),
            )
            entry["last_success_at"] = recorded_at
            return
        entry["failures"] = int(entry.get("failures", 0)) + 1
        if outcome.get("timed_out"):
            entry["timeouts"] = int(entry.get("timeouts", 0)) + 1
        entry["last_failure_at"] = recorded_at

    def _rolling_average(self, current: float, value: float, sample_count: int) -> float:
        if sample_count <= 1:
            return round(value, 3)
        return round(((current * (sample_count - 1)) + value) / sample_count, 3)

    def _source_priority_key(
        self,
        source: Dict[str, Any],
        summary_by_source_id: Dict[str, Dict[str, Any]] | None = None,
    ) -> tuple:
        source_id = str(source.get("source_id") or "").strip()
        summary = {}
        if summary_by_source_id is not None:
            summary = dict(summary_by_source_id.get(source_id) or {})
        with self._health_lock:
            entry = dict(self._source_health.get(source_id) or {})
        supports_download_rank = 0 if summary.get("supports_download", False) else 1
        if not entry:
            return (supports_download_rank, 1, 0, float("inf"), 0.0)
        successes = max(0, int(entry.get("successes", 0) or 0))
        failures = max(0, int(entry.get("failures", 0) or 0))
        timeouts = max(0, int(entry.get("timeouts", 0) or 0))
        avg_duration_ms = float(entry.get("avg_duration_ms", 0.0) or 0.0) or float("inf")
        avg_success_ms = float(entry.get("avg_success_ms", 0.0) or 0.0) or avg_duration_ms
        last_success_at = float(entry.get("last_success_at", 0.0) or 0.0)
        last_failure_at = float(entry.get("last_failure_at", 0.0) or 0.0)
        if successes > 0:
            penalty = timeouts * 2 + max(0, failures - successes)
            return (supports_download_rank, 0, penalty, avg_success_ms, -last_success_at)
        return (
            supports_download_rank,
            2,
            timeouts * 2 + failures,
            avg_duration_ms,
            last_failure_at,
        )

    def _load_source_health(self) -> dict[str, dict[str, Any]]:
        if self._health_path is None or not self._health_path.exists():
            return {}
        try:
            with open(self._health_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        sources = payload.get("sources", payload)
        if not isinstance(sources, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for source_id, item in sources.items():
            if not isinstance(item, dict):
                continue
            normalized[str(source_id)] = {
                "attempts": max(0, int(item.get("attempts", 0) or 0)),
                "successes": max(0, int(item.get("successes", 0) or 0)),
                "failures": max(0, int(item.get("failures", 0) or 0)),
                "timeouts": max(0, int(item.get("timeouts", 0) or 0)),
                "avg_duration_ms": float(item.get("avg_duration_ms", 0.0) or 0.0),
                "avg_success_ms": float(item.get("avg_success_ms", 0.0) or 0.0),
                "last_success_at": float(item.get("last_success_at", 0.0) or 0.0),
                "last_failure_at": float(item.get("last_failure_at", 0.0) or 0.0),
            }
        return normalized

    def _save_source_health_locked(self) -> None:
        if self._health_path is None:
            return
        self._health_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._health_path.with_suffix(self._health_path.suffix + ".tmp")
        payload = {
            "schema_version": self._HEALTH_SCHEMA_VERSION,
            "updated_at": time.time(),
            "sources": self._source_health,
        }
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._health_path)
