from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolRenderConfig:
    max_tool_response_chars: int = 2800
    max_tool_preview_items: int = 8
    max_tool_preview_text: int = 180


class ToolResultRenderer:
    def __init__(
        self,
        reports_dir: str | Path,
        source_registry: Any,
        manager: Any,
        config: ToolRenderConfig,
    ):
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.source_registry = source_registry
        self.manager = manager
        self.config = config

    def to_json_text(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def truncate_text(self, value: Any, limit: int | None = None) -> str:
        text = str(value or "")
        max_length = limit or self.config.max_tool_preview_text
        if len(text) <= max_length:
            return text
        return text[: max(1, max_length - 1)] + "…"

    def render_source_change_summary(self, action: str, source: dict[str, Any]) -> str:
        summary = {
            "action": action,
            "registry_path": str(self.source_registry.registry_path),
            "source": self._compact_source(source),
        }
        return self.to_json_text(summary)

    def render_clean_rule_import_summary(self, record: dict[str, Any]) -> str:
        return self.to_json_text(
            {
                "repo_id": record.get("repo_id", ""),
                "name": record.get("name", ""),
                "source_ref": record.get("source_ref", ""),
                "imported_at": record.get("imported_at", 0),
                "rule_count": record.get("rule_count", 0),
                "enabled_rule_count": record.get("enabled_rule_count", 0),
                "scoped_rule_count": record.get("scoped_rule_count", 0),
                "skipped_rule_count": record.get("skipped_rule_count", 0),
                "path": record.get("path", ""),
            }
        )

    def render_clean_rule_list_summary(
        self,
        repos: list[dict[str, Any]],
        limit: int,
        offset: int,
    ) -> str:
        total = len(repos)
        sliced = repos[offset : offset + limit]
        preview_count = min(self.config.max_tool_preview_items, len(sliced))
        return self.to_json_text(
            {
                "total_count": total,
                "offset": offset,
                "limit": limit,
                "returned_count": len(sliced),
                "previewed_count": preview_count,
                "has_more": offset + len(sliced) < total,
                "next_offset": offset + len(sliced) if offset + len(sliced) < total else None,
                "repositories": [self._compact_clean_rule_repo(item) for item in sliced[:preview_count]],
                "omitted_from_inline_count": max(0, len(sliced) - preview_count),
            }
        )

    def render_import_summary(self, result: dict[str, Any]) -> str:
        sources = list(result.get("sources") or [])
        warnings = list(result.get("warnings") or [])
        source_preview_count = min(self.config.max_tool_preview_items, len(sources))
        warning_preview_count = min(self.config.max_tool_preview_items, len(warnings))
        report_path = ""

        while True:
            summary = {
                "imported_count": result.get("imported_count", 0),
                "supported_search_count": result.get("supported_search_count", 0),
                "supported_download_count": result.get("supported_download_count", 0),
                "warning_count": len(warnings),
                "source_count": len(sources),
                "registry_path": str(self.source_registry.registry_path),
                "raw_dir": str(self.source_registry.raw_dir),
                "normalized_dir": str(self.source_registry.normalized_dir),
                "sources_preview": [self._compact_source(item) for item in sources[:source_preview_count]],
                "warnings_preview": [
                    self.truncate_text(item, self.config.max_tool_preview_text)
                    for item in warnings[:warning_preview_count]
                ],
                "queued_probe_count": int(result.get("queued_probe_count", 0) or 0),
                "probe_queue_size": int(result.get("probe_queue_size", 0) or 0),
                "remaining_source_count": max(0, len(sources) - source_preview_count),
                "remaining_warning_count": max(0, len(warnings) - warning_preview_count),
            }
            if report_path:
                summary["report_path"] = report_path
            text = self.to_json_text(summary)
            if len(text) <= self.config.max_tool_response_chars:
                if (
                    len(sources) > source_preview_count
                    or len(warnings) > warning_preview_count
                ) and not report_path:
                    report_path = self._write_json_report("import-sources", result)
                    continue
                return text
            if not report_path:
                report_path = self._write_json_report("import-sources", result)
                continue
            if warning_preview_count > 0:
                warning_preview_count -= 1
                continue
            if source_preview_count > 1:
                source_preview_count -= 1
                continue
            return text

    def render_sources_summary(
        self,
        sources: list[dict[str, Any]],
        enabled_only: bool,
        limit: int,
        offset: int,
    ) -> str:
        total = len(sources)
        sliced = sources[offset : offset + limit]
        preview_count = min(self.config.max_tool_preview_items, len(sliced))
        report_path = ""

        while True:
            summary = {
                "total_count": total,
                "enabled_only": enabled_only,
                "enabled_count": sum(1 for item in sources if item.get("enabled")),
                "disabled_count": sum(1 for item in sources if not item.get("enabled")),
                "offset": offset,
                "limit": limit,
                "returned_count": len(sliced),
                "requested_count": len(sliced),
                "previewed_count": preview_count,
                "has_more": offset + len(sliced) < total,
                "next_offset": offset + len(sliced) if offset + len(sliced) < total else None,
                "registry_path": str(self.source_registry.registry_path),
                "sources": [self._compact_source(item) for item in sliced[:preview_count]],
                "omitted_from_inline_count": max(0, len(sliced) - preview_count),
            }
            if report_path:
                summary["report_path"] = report_path
            text = self.to_json_text(summary)
            if len(text) <= self.config.max_tool_response_chars:
                if len(sliced) > preview_count and not report_path:
                    report_path = self._write_json_report(
                        "list-sources",
                        {
                            "total_count": total,
                            "enabled_only": enabled_only,
                            "offset": offset,
                            "limit": limit,
                            "requested_count": len(sliced),
                            "registry_path": str(self.source_registry.registry_path),
                            "sources": sliced,
                        },
                    )
                    continue
                return text
            if not report_path:
                report_path = self._write_json_report(
                    "list-sources",
                    {
                        "total_count": total,
                        "enabled_only": enabled_only,
                        "offset": offset,
                        "limit": limit,
                        "requested_count": len(sliced),
                        "registry_path": str(self.source_registry.registry_path),
                        "sources": sliced,
                    },
                )
                continue
            if preview_count > 1:
                preview_count -= 1
                continue
            return text

    def render_search_summary(self, result: dict[str, Any]) -> str:
        return self.render_search_summary_with_cache(result, {})

    def render_search_summary_with_cache(
        self,
        result: dict[str, Any],
        cache_record: dict[str, Any],
    ) -> str:
        results = list(result.get("results") or [])
        skipped_sources = list(result.get("skipped_sources") or [])
        errors = list(result.get("errors") or [])
        result_preview_count = min(self.config.max_tool_preview_items, len(results))
        skipped_preview_count = min(self.config.max_tool_preview_items, len(skipped_sources))
        error_preview_count = min(self.config.max_tool_preview_items, len(errors))
        report_path = ""

        while True:
            compact = {
                "keyword": result.get("keyword", ""),
                "search_id": cache_record.get("search_id", ""),
                "search_path": cache_record.get("path", ""),
                "candidate_sources": result.get("candidate_sources", result.get("searched_sources", 0)),
                "searched_sources": result.get("searched_sources", 0),
                "completed_sources": result.get("completed_sources", 0),
                "successful_sources": result.get("successful_sources", 0),
                "partial": bool(result.get("partial", False)),
                "early_stopped": bool(result.get("early_stopped", False)),
                "stop_reason": result.get("stop_reason", ""),
                "timed_out_source_count": result.get("timed_out_source_count", 0),
                "unsearched_source_count": result.get("unsearched_source_count", 0),
                "result_count": len(results),
                "skipped_source_count": len(skipped_sources),
                "error_count": len(errors),
                "results": [
                    self._compact_search_result(item, index)
                    for index, item in enumerate(results[:result_preview_count])
                ],
                "skipped_sources": [
                    self._compact_search_notice(item, "reason")
                    for item in skipped_sources[:skipped_preview_count]
                ],
                "errors": [
                    self._compact_search_notice(item, "error") for item in errors[:error_preview_count]
                ],
                "remaining_result_count": max(0, len(results) - result_preview_count),
                "remaining_skipped_count": max(0, len(skipped_sources) - skipped_preview_count),
                "remaining_error_count": max(0, len(errors) - error_preview_count),
            }
            if report_path:
                compact["report_path"] = report_path
            text = self.to_json_text(compact)
            if len(text) <= self.config.max_tool_response_chars:
                if (
                    len(results) > result_preview_count
                    or len(skipped_sources) > skipped_preview_count
                    or len(errors) > error_preview_count
                ) and not report_path:
                    report_path = self._write_json_report("search-books", result)
                    continue
                return text
            if not report_path:
                report_path = self._write_json_report("search-books", result)
                continue
            if error_preview_count > 0:
                error_preview_count -= 1
                continue
            if skipped_preview_count > 0:
                skipped_preview_count -= 1
                continue
            if result_preview_count > 1:
                result_preview_count -= 1
                continue
            return text

    def render_search_cache_list_summary(
        self,
        searches: list[dict[str, Any]],
        limit: int,
        offset: int,
    ) -> str:
        total = len(searches)
        sliced = searches[offset : offset + limit]
        preview_count = min(self.config.max_tool_preview_items, len(sliced))
        payload = {
            "total_count": total,
            "offset": offset,
            "limit": limit,
            "returned_count": len(sliced),
            "previewed_count": preview_count,
            "has_more": offset + len(sliced) < total,
            "next_offset": offset + len(sliced) if offset + len(sliced) < total else None,
            "searches": [self._compact_search_record(item) for item in sliced[:preview_count]],
            "omitted_from_inline_count": max(0, len(sliced) - preview_count),
        }
        return self.to_json_text(payload)

    def render_cached_search_results(
        self,
        cached_payload: dict[str, Any],
        limit: int,
        offset: int,
    ) -> str:
        record = dict(cached_payload.get("record") or {})
        result = dict(cached_payload.get("result") or {})
        results = list(result.get("results") or [])
        sliced = results[offset : offset + limit]
        preview_count = min(self.config.max_tool_preview_items, len(sliced))
        payload = {
            "search_id": record.get("search_id", ""),
            "keyword": record.get("keyword", result.get("keyword", "")),
            "created_at": record.get("created_at", 0),
            "search_path": record.get("path", ""),
            "total_result_count": len(results),
            "offset": offset,
            "limit": limit,
            "returned_count": len(sliced),
            "previewed_count": preview_count,
            "has_more": offset + len(sliced) < len(results),
            "next_offset": offset + len(sliced) if offset + len(sliced) < len(results) else None,
            "results": [
                self._compact_search_result(item, offset + index)
                for index, item in enumerate(sliced[:preview_count])
            ],
            "omitted_from_inline_count": max(0, len(sliced) - preview_count),
        }
        return self.to_json_text(payload)

    def render_auto_download_summary(
        self,
        orchestration: dict[str, Any],
        cache_record: dict[str, Any],
        job_status: dict[str, Any],
    ) -> str:
        attempts = list(orchestration.get("attempts") or [])
        skipped_candidates = list(orchestration.get("skipped_candidates") or [])
        attempt_preview_count = min(self.config.max_tool_preview_items, len(attempts))
        skipped_preview_count = min(self.config.max_tool_preview_items, len(skipped_candidates))
        report_path = ""

        while True:
            summary = {
                "status": orchestration.get("status", ""),
                "failure_reason": orchestration.get("failure_reason", ""),
                "keyword": orchestration.get("keyword", ""),
                "author": orchestration.get("author", ""),
                "search_id": cache_record.get("search_id", ""),
                "search_path": cache_record.get("path", ""),
                "candidate_sources": orchestration.get("search_result", {}).get(
                    "candidate_sources",
                    0,
                ),
                "searched_sources": orchestration.get("search_result", {}).get(
                    "searched_sources",
                    0,
                ),
                "successful_sources": orchestration.get("search_result", {}).get(
                    "successful_sources",
                    0,
                ),
                "result_count": orchestration.get("search_result", {}).get("result_count", 0),
                "candidate_count": orchestration.get("candidate_count", 0),
                "skipped_candidate_count": orchestration.get("skipped_candidate_count", 0),
                "attempt_limit": orchestration.get("attempt_limit", 0),
                "attempted_count": orchestration.get("attempted_count", 0),
                "selected": self._compact_auto_download_candidate(
                    dict(orchestration.get("selected") or {}),
                ),
                "job": self._compact_auto_download_job(
                    dict(orchestration.get("job") or {}),
                    job_status,
                ),
                "attempts": [
                    self._compact_auto_download_attempt(item)
                    for item in attempts[:attempt_preview_count]
                ],
                "skipped_candidates": [
                    self._compact_auto_download_candidate(item, include_skip_reason=True)
                    for item in skipped_candidates[:skipped_preview_count]
                ],
                "omitted_attempt_count": max(0, len(attempts) - attempt_preview_count),
                "omitted_skipped_candidate_count": max(
                    0,
                    len(skipped_candidates) - skipped_preview_count,
                ),
            }
            if report_path:
                summary["report_path"] = report_path
            text = self.to_json_text(summary)
            if len(text) <= self.config.max_tool_response_chars:
                if (
                    len(attempts) > attempt_preview_count
                    or len(skipped_candidates) > skipped_preview_count
                ) and not report_path:
                    report_path = self._write_json_report(
                        "auto-download",
                        {
                            "orchestration": orchestration,
                            "search_record": cache_record,
                            "job_status": job_status,
                        },
                    )
                    continue
                return text
            if not report_path:
                report_path = self._write_json_report(
                    "auto-download",
                    {
                        "orchestration": orchestration,
                        "search_record": cache_record,
                        "job_status": job_status,
                    },
                )
                continue
            if skipped_preview_count > 0:
                skipped_preview_count -= 1
                continue
            if attempt_preview_count > 1:
                attempt_preview_count -= 1
                continue
            return text

    def render_jobs_summary(self, jobs: list[dict[str, Any]], limit: int, offset: int) -> str:
        total = len(jobs)
        sliced = jobs[offset : offset + limit]
        preview_count = min(self.config.max_tool_preview_items, len(sliced))
        report_path = ""

        while True:
            summary = {
                "total_count": total,
                "offset": offset,
                "limit": limit,
                "returned_count": len(sliced),
                "requested_count": len(sliced),
                "previewed_count": preview_count,
                "has_more": offset + len(sliced) < total,
                "next_offset": offset + len(sliced) if offset + len(sliced) < total else None,
                "jobs_dir": str(self.manager.jobs_dir),
                "jobs": [self._compact_job(item) for item in sliced[:preview_count]],
                "omitted_from_inline_count": max(0, len(sliced) - preview_count),
            }
            if report_path:
                summary["report_path"] = report_path
            text = self.to_json_text(summary)
            if len(text) <= self.config.max_tool_response_chars:
                if len(sliced) > preview_count and not report_path:
                    report_path = self._write_json_report(
                        "list-jobs",
                        {
                            "total_count": total,
                            "offset": offset,
                            "limit": limit,
                            "requested_count": len(sliced),
                            "jobs_dir": str(self.manager.jobs_dir),
                            "jobs": sliced,
                        },
                    )
                    continue
                return text
            if not report_path:
                report_path = self._write_json_report(
                    "list-jobs",
                    {
                        "total_count": total,
                        "offset": offset,
                        "limit": limit,
                        "requested_count": len(sliced),
                        "jobs_dir": str(self.manager.jobs_dir),
                        "jobs": sliced,
                    },
                )
                continue
            if preview_count > 1:
                preview_count -= 1
                continue
            return text

    def render_status(self, status: dict[str, Any], created: bool) -> str:
        prefix = "已创建并启动任务" if created else "任务状态"
        lines = [
            f"{prefix}: {status['job_id']}",
            f"书名: {status['book_name']}",
            f"状态: {status['state']}",
            f"进度: {status['completed_chapters']}/{status['total_chapters']}",
            f"输出: {status['output_path']}",
            f"Journal: {status['journal_path']}",
        ]
        if status.get("latest_errors"):
            first_error = status["latest_errors"][0]
            lines.append(
                f"最近错误: index={first_error['index']} {first_error['title']} -> {first_error['error']}"
            )
        if status.get("corrupt_lines"):
            lines.append(f"警告: journal 中检测到 {status['corrupt_lines']} 行损坏记录")
        return "\n".join(lines)

    def _write_json_report(self, prefix: str, payload: Any) -> str:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        report_path = self.reports_dir / "{prefix}-{timestamp}-{ms}.json".format(
            prefix=prefix,
            timestamp=timestamp,
            ms=int(time.time() * 1000) % 1000,
        )
        with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        return str(report_path)

    def _compact_source(self, item: dict[str, Any]) -> dict[str, Any]:
        compact = {
            "source_id": item.get("source_id", ""),
            "name": item.get("name", ""),
            "enabled": bool(item.get("enabled", False)),
            "search_uses_js": bool(item.get("search_uses_js", False)),
            "download_uses_js": bool(item.get("download_uses_js", False)),
            "has_js_lib": bool(item.get("has_js_lib", False)),
            "has_login_flow": bool(item.get("has_login_flow", False)),
            "supports_search": bool(item.get("supports_search", False)),
            "supports_download": bool(item.get("supports_download", False)),
            "group": self.truncate_text(item.get("group", ""), 60),
            "search_url": self.truncate_text(item.get("search_url", ""), 120),
            "issues": [self.truncate_text(issue, 80) for issue in list(item.get("issues") or [])[:3]],
        }
        for stage in ("search", "preflight", "download"):
            state_key = "{stage}_health_state".format(stage=stage)
            summary_key = "{stage}_health_summary".format(stage=stage)
            updated_key = "{stage}_health_updated_at".format(stage=stage)
            if state_key in item:
                compact[state_key] = item.get(state_key, "")
            if summary_key in item:
                compact[summary_key] = self.truncate_text(item.get(summary_key, ""), 100)
            if updated_key in item:
                compact[updated_key] = item.get(updated_key, 0)
        return compact

    def _compact_search_result(self, item: dict[str, Any], result_index: int | None = None) -> dict[str, Any]:
        compact = {
            "source_id": item.get("source_id", ""),
            "source_name": item.get("source_name", ""),
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "book_url": item.get("book_url", ""),
            "kind": self.truncate_text(item.get("kind", ""), 40),
            "last_chapter": self.truncate_text(item.get("last_chapter", ""), 60),
            "word_count": item.get("word_count", ""),
            "intro": self.truncate_text(item.get("intro", ""), self.config.max_tool_preview_text),
        }
        if result_index is not None:
            compact["result_index"] = result_index
        return compact

    def _compact_search_record(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "search_id": item.get("search_id", ""),
            "keyword": item.get("keyword", ""),
            "created_at": item.get("created_at", 0),
            "searched_sources": item.get("searched_sources", 0),
            "successful_sources": item.get("successful_sources", 0),
            "result_count": item.get("result_count", 0),
            "error_count": item.get("error_count", 0),
            "search_path": item.get("path", ""),
        }

    def _compact_search_notice(self, item: dict[str, Any], message_key: str) -> dict[str, Any]:
        return {
            "source_id": item.get("source_id", ""),
            "source_name": item.get("source_name", ""),
            message_key: self.truncate_text(item.get(message_key, ""), self.config.max_tool_preview_text),
        }

    def _compact_auto_download_candidate(
        self,
        item: dict[str, Any],
        include_skip_reason: bool = False,
    ) -> dict[str, Any]:
        if not item:
            return {}
        compact = {
            "candidate_index": item.get("candidate_index", 0),
            "source_id": item.get("source_id", ""),
            "source_name": item.get("source_name", ""),
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "book_url": item.get("book_url", ""),
            "supports_download": bool(item.get("supports_download", False)),
            "title_match": item.get("title_match", ""),
            "author_match": item.get("author_match", ""),
            "search_health_state": item.get("search_health_state", ""),
            "preflight_health_state": item.get("preflight_health_state", ""),
            "download_health_state": item.get("download_health_state", ""),
            "source_issues": [
                self.truncate_text(issue, 80)
                for issue in list(item.get("source_issues") or [])[:3]
            ],
        }
        if include_skip_reason:
            compact["skip_reason"] = self.truncate_text(item.get("skip_reason", ""), 120)
        return compact

    def _compact_auto_download_attempt(self, item: dict[str, Any]) -> dict[str, Any]:
        compact = {
            "attempt_index": item.get("attempt_index", 0),
            "source_id": item.get("source_id", ""),
            "source_name": item.get("source_name", ""),
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "book_url": item.get("book_url", ""),
            "outcome": item.get("outcome", ""),
            "elapsed_ms": item.get("elapsed_ms", 0),
        }
        if item.get("toc_count"):
            compact["toc_count"] = item.get("toc_count", 0)
        if item.get("job_id"):
            compact["job_id"] = item.get("job_id", "")
        if item.get("error"):
            compact["error"] = self.truncate_text(item.get("error", ""), 120)
        return compact

    def _compact_auto_download_job(
        self,
        job: dict[str, Any],
        job_status: dict[str, Any],
    ) -> dict[str, Any]:
        if job_status:
            return self._compact_job(job_status)
        if not job:
            return {}
        status = dict(job.get("status") or {})
        if status:
            return self._compact_job(status)
        return {
            "job_id": job.get("job_id", ""),
            "book_name": job.get("book_name", ""),
            "state": "",
            "completed_chapters": 0,
            "total_chapters": job.get("toc_count", 0),
            "output_path": "",
            "journal_path": "",
        }

    def _compact_job(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": item.get("job_id", ""),
            "book_name": item.get("book_name", ""),
            "state": item.get("state", ""),
            "completed_chapters": item.get("completed_chapters", 0),
            "total_chapters": item.get("total_chapters", 0),
            "output_path": item.get("output_path", ""),
            "journal_path": item.get("journal_path", ""),
        }

    def _compact_clean_rule_repo(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "repo_id": item.get("repo_id", ""),
            "name": item.get("name", ""),
            "source_ref": self.truncate_text(item.get("source_ref", ""), 120),
            "imported_at": item.get("imported_at", 0),
            "rule_count": item.get("rule_count", 0),
            "enabled_rule_count": item.get("enabled_rule_count", 0),
            "scoped_rule_count": item.get("scoped_rule_count", 0),
            "skipped_rule_count": item.get("skipped_rule_count", 0),
            "path": item.get("path", ""),
        }
