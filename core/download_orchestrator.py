from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from .book_resolution_service import BookResolutionService
from .source_downloader import SourceDownloadService


@dataclass
class DownloadOrchestratorConfig:
    default_attempt_limit: int = 5


class DownloadOrchestrator:
    def __init__(
        self,
        resolver: BookResolutionService,
        source_download_service: SourceDownloadService,
        config: Optional[DownloadOrchestratorConfig] = None,
    ):
        self.resolver = resolver
        self.source_download_service = source_download_service
        self.config = config or DownloadOrchestratorConfig()

    def auto_download(
        self,
        keyword: str,
        author: str = "",
        source_ids: Optional[Iterable[str]] = None,
        search_limit: int = 20,
        include_disabled: bool = False,
        attempt_limit: int = 0,
        output_filename: str = "",
    ) -> Dict[str, Any]:
        resolution = self.resolver.resolve(
            keyword,
            author,
            source_ids,
            search_limit,
            include_disabled,
        )
        candidates = list(resolution.get("candidates") or [])
        effective_attempt_limit = max(
            1,
            int(attempt_limit or 0) or int(self.config.default_attempt_limit),
        )
        attempts: list[dict[str, Any]] = []
        if not candidates:
            failure_reason = "没有搜索到可用结果"
            status = "no_candidates"
            if int(resolution.get("skipped_candidate_count", 0) or 0) > 0:
                failure_reason = "搜索结果存在，但都不可自动下载"
                status = "no_attemptable_candidates"
            return self._build_result(
                status,
                resolution,
                effective_attempt_limit,
                attempts,
                {},
                {},
                failure_reason,
            )

        for attempt_index, candidate in enumerate(candidates[:effective_attempt_limit]):
            started_at = time.monotonic()
            source_id = str(candidate.get("source_id") or "").strip()
            book_url = str(candidate.get("book_url") or "").strip()
            book_name = str(candidate.get("title") or resolution.get("keyword") or "").strip()
            try:
                preflight = self.source_download_service.preflight_book(
                    source_id,
                    book_url,
                    book_name,
                )
            except Exception as exc:
                attempts.append(
                    {
                        "attempt_index": attempt_index,
                        "source_id": source_id,
                        "source_name": str(candidate.get("source_name") or source_id).strip(),
                        "title": book_name,
                        "author": str(candidate.get("author") or "").strip(),
                        "book_url": book_url,
                        "outcome": "preflight_failed",
                        "error": str(exc),
                        "elapsed_ms": round((time.monotonic() - started_at) * 1000.0, 3),
                    }
                )
                continue

            preflight_elapsed_ms = round((time.monotonic() - started_at) * 1000.0, 3)
            try:
                job = self.source_download_service.create_job_from_plan(preflight, output_filename)
            except Exception as exc:
                attempts.append(
                    {
                        "attempt_index": attempt_index,
                        "source_id": source_id,
                        "source_name": str(candidate.get("source_name") or source_id).strip(),
                        "title": book_name,
                        "author": str(candidate.get("author") or "").strip(),
                        "book_url": book_url,
                        "outcome": "job_create_failed",
                        "error": str(exc),
                        "elapsed_ms": preflight_elapsed_ms,
                        "preflight": preflight,
                    }
                )
                return self._build_result(
                    "job_create_failed",
                    resolution,
                    effective_attempt_limit,
                    attempts,
                    dict(candidate),
                    {},
                    str(exc),
                )

            attempts.append(
                {
                    "attempt_index": attempt_index,
                    "source_id": source_id,
                    "source_name": str(candidate.get("source_name") or source_id).strip(),
                    "title": book_name,
                    "author": str(candidate.get("author") or "").strip(),
                    "book_url": book_url,
                    "outcome": "started",
                    "elapsed_ms": preflight_elapsed_ms,
                    "preflight": preflight,
                    "job_id": job.get("job_id", ""),
                    "toc_count": int(preflight.get("toc_count", 0) or 0),
                }
            )
            return self._build_result(
                "started",
                resolution,
                effective_attempt_limit,
                attempts,
                dict(candidate),
                job,
                "",
            )

        return self._build_result(
            "all_preflight_failed",
            resolution,
            effective_attempt_limit,
            attempts,
            {},
            {},
            "候选书源都未通过目录预检",
        )

    def _build_result(
        self,
        status: str,
        resolution: dict[str, Any],
        attempt_limit: int,
        attempts: list[dict[str, Any]],
        selected: dict[str, Any],
        job: dict[str, Any],
        failure_reason: str,
    ) -> dict[str, Any]:
        search_result = dict(resolution.get("search_result") or {})
        return {
            "status": status,
            "failure_reason": str(failure_reason or "").strip(),
            "keyword": resolution.get("keyword", ""),
            "author": resolution.get("author", ""),
            "source_ids": list(resolution.get("source_ids") or []),
            "include_disabled": bool(resolution.get("include_disabled", False)),
            "search_limit": int(resolution.get("limit", 0) or 0),
            "candidate_count": int(resolution.get("candidate_count", 0) or 0),
            "skipped_candidate_count": int(resolution.get("skipped_candidate_count", 0) or 0),
            "search_result": search_result,
            "candidates": list(resolution.get("candidates") or []),
            "skipped_candidates": list(resolution.get("skipped_candidates") or []),
            "attempt_limit": attempt_limit,
            "attempted_count": len(attempts),
            "attempts": attempts,
            "selected": selected,
            "job": job,
        }
