from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .rule_engine import RuleEngine, RuleEngineError
from .search_stats_store import SearchStatsStore
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
        source_profile_service: Any = None,
        source_health_store: Any = None,
    ):
        self.registry = registry
        self.engine = engine
        self.config = config or SearchServiceConfig()
        self.source_profile_service = source_profile_service
        self.source_health_store = source_health_store
        self._health_lock = threading.RLock()
        health_path = self.config.health_path
        self._health_path = Path(health_path) if health_path else None
        self._stats_store = (
            SearchStatsStore(self._health_path) if self._health_path else None
        )
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

        runtime_health = self._load_runtime_health(source_summaries)
        searchable_summaries = [
            item
            for item in source_summaries
            if self._search_skip_reason(
                item, runtime_health.get(str(item.get("source_id") or ""), {})
            )
            == ""
        ]
        summary_by_source_id = {
            str(item.get("source_id") or ""): item for item in searchable_summaries
        }
        skipped_sources = [
            {
                "source_id": item.get("source_id", ""),
                "source_name": item.get("name", ""),
                "reason": self._search_skip_reason(
                    item,
                    runtime_health.get(str(item.get("source_id") or ""), {}),
                ),
            }
            for item in source_summaries
            if self._search_skip_reason(
                item,
                runtime_health.get(str(item.get("source_id") or ""), {}),
            )
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
            key=lambda item: self._source_priority_key(
                item, summary_by_source_id, runtime_health
            ),
        )

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        max_workers = min(max(1, self.config.max_workers), len(sources))
        dispatch_window = (
            max_workers if max_workers <= 1 else min(len(sources), max_workers * 2)
        )
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
                            summary_by_source_id.get(
                                str(source.get("source_id") or ""), {}
                            ),
                            runtime_health.get(str(source.get("source_id") or ""), {}),
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
        health_entry: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        enriched: List[Dict[str, Any]] = []
        supports_download = self._supports_download_with_runtime(summary, health_entry)
        issues = [
            str(issue).strip()
            for issue in list(summary.get("issues") or [])
            if str(issue).strip()
        ]
        for item in items:
            current = dict(item)
            current["supports_download"] = supports_download
            current["static_supports_download"] = bool(
                summary.get("supports_download", False)
            )
            for stage in ("search", "preflight", "download"):
                stage_entry = dict(health_entry.get(stage) or {})
                current["{stage}_health_state".format(stage=stage)] = str(
                    stage_entry.get("state", "unknown") or "unknown"
                )
                current["{stage}_health_summary".format(stage=stage)] = (
                    self._stage_summary(stage_entry)
                )
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

    def _should_stop_early(
        self, results: list[Dict[str, Any]], keyword: str, limit: int
    ) -> bool:
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
        if self._stats_store is not None:
            updated_entries = self._stats_store.apply_outcomes(outcomes)
            if updated_entries:
                with self._health_lock:
                    self._source_health.update(updated_entries)
            return
        with self._health_lock:
            for item in outcomes:
                self._apply_source_outcome(item)

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

    def _rolling_average(
        self, current: float, value: float, sample_count: int
    ) -> float:
        if sample_count <= 1:
            return round(value, 3)
        return round(((current * (sample_count - 1)) + value) / sample_count, 3)

    def _source_priority_key(
        self,
        source: Dict[str, Any],
        summary_by_source_id: Dict[str, Dict[str, Any]] | None = None,
        runtime_health_by_source: Dict[str, Dict[str, Any]] | None = None,
    ) -> tuple:
        source_id = str(source.get("source_id") or "").strip()
        summary = {}
        if summary_by_source_id is not None:
            summary = dict(summary_by_source_id.get(source_id) or {})
        with self._health_lock:
            entry = dict(self._source_health.get(source_id) or {})
        runtime_health = {}
        if runtime_health_by_source is not None:
            runtime_health = dict(runtime_health_by_source.get(source_id) or {})
        else:
            runtime_health = self._get_runtime_health_entry(source_id)
        profile_rank = self._profile_priority_rank(source_id)
        supports_download_rank = 0 if summary.get("supports_download", False) else 1
        runtime_rank = self._search_health_rank(runtime_health.get("search", {}))
        if not entry:
            return (
                runtime_rank,
                supports_download_rank,
                profile_rank,
                1,
                0,
                float("inf"),
                0.0,
            )
        successes = max(0, int(entry.get("successes", 0) or 0))
        failures = max(0, int(entry.get("failures", 0) or 0))
        timeouts = max(0, int(entry.get("timeouts", 0) or 0))
        avg_duration_ms = float(entry.get("avg_duration_ms", 0.0) or 0.0) or float(
            "inf"
        )
        avg_success_ms = (
            float(entry.get("avg_success_ms", 0.0) or 0.0) or avg_duration_ms
        )
        last_success_at = float(entry.get("last_success_at", 0.0) or 0.0)
        last_failure_at = float(entry.get("last_failure_at", 0.0) or 0.0)
        if successes > 0:
            penalty = timeouts * 2 + max(0, failures - successes)
            return (
                runtime_rank,
                supports_download_rank,
                profile_rank,
                0,
                penalty,
                avg_success_ms,
                -last_success_at,
            )
        return (
            runtime_rank,
            supports_download_rank,
            profile_rank,
            2,
            timeouts * 2 + failures,
            avg_duration_ms,
            last_failure_at,
        )

    def _load_runtime_health(
        self, source_summaries: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        if self.source_health_store is None:
            return {}
        source_ids = [
            str(item.get("source_id") or "").strip()
            for item in source_summaries
            if str(item.get("source_id") or "").strip()
        ]
        try:
            return dict(self.source_health_store.get_many(source_ids))
        except Exception:
            return {}

    def _get_runtime_health_entry(self, source_id: str) -> Dict[str, Any]:
        if self.source_health_store is None or not source_id:
            return {}
        try:
            return dict(self.source_health_store.get_source_health(source_id) or {})
        except Exception:
            return {}

    def _search_skip_reason(
        self, summary: Dict[str, Any], health_entry: Dict[str, Any]
    ) -> str:
        if not summary.get("supports_search"):
            return (
                "；".join(summary.get("issues") or []) or "当前书源不支持 route A 搜索"
            )
        stage_entry = dict(health_entry.get("search") or {})
        state = str(stage_entry.get("state", "unknown") or "unknown")
        if state == "unsupported":
            return self._stage_summary(stage_entry)
        return ""

    def _supports_download_with_runtime(
        self,
        summary: Dict[str, Any],
        health_entry: Dict[str, Any],
    ) -> bool:
        if not bool(summary.get("supports_download", False)):
            return False
        for stage in ("preflight", "download"):
            stage_state = str(
                (health_entry.get(stage) or {}).get("state", "unknown") or "unknown"
            )
            if stage_state == "unsupported":
                return False
        return True

    def _stage_summary(self, stage_entry: Dict[str, Any]) -> str:
        note = str(stage_entry.get("note", "") or "").strip()
        if note:
            return note
        error_summary = str(stage_entry.get("last_error_summary", "") or "").strip()
        if error_summary:
            return error_summary
        state = str(stage_entry.get("state", "unknown") or "unknown")
        if state == "healthy":
            return "最近探测成功"
        if state == "degraded":
            return "最近探测不稳定"
        if state == "broken":
            return "最近探测失败"
        if state == "unsupported":
            return "静态或运行时规则不支持"
        return "尚无健康记录"

    def _search_health_rank(self, stage_entry: Dict[str, Any]) -> int:
        state = str(stage_entry.get("state", "unknown") or "unknown")
        return {
            "healthy": 0,
            "degraded": 1,
            "unknown": 2,
            "broken": 3,
            "unsupported": 4,
        }.get(state, 5)

    def _profile_priority_rank(self, source_id: str) -> int:
        if self.source_profile_service is None or not source_id:
            return 1
        try:
            profile = dict(self.source_profile_service.get(source_id) or {})
        except Exception:
            return 1
        preferred = str((profile.get("preferred_extractors") or [""])[0] or "").strip()
        if preferred.startswith("template_"):
            return 0
        if preferred == "fallback_rule":
            return 1
        if "javascript" in preferred:
            return 3
        if not preferred:
            return 2
        return 2

    def _load_source_health(self) -> dict[str, dict[str, Any]]:
        if self._stats_store is not None:
            try:
                return self._stats_store.load_all()
            except Exception:
                return {}
        if self._health_path is None:
            return {}
        return {}
