from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from .book_resolution_service import BookResolutionService
from .source_downloader import SourceDownloadService


@dataclass
class DownloadOrchestratorConfig:
    default_attempt_limit: int = 12
    group_preflight_workers: int = 4


class DownloadOrchestrator:
    def __init__(
        self,
        resolver: BookResolutionService,
        source_download_service: SourceDownloadService,
        config: Optional[DownloadOrchestratorConfig] = None,
        source_profile_service: Any = None,
    ):
        self.resolver = resolver
        self.source_download_service = source_download_service
        self.config = config or DownloadOrchestratorConfig()
        self.source_profile_service = source_profile_service

    def download_candidate(
        self,
        candidate: Dict[str, Any],
        output_filename: str = "",
    ) -> Dict[str, Any]:
        """精确下载某一个已知候选（带 book_url），不再按书名重新搜索。

        复用与 ``auto_download`` 相同的 preflight→sample→create_job 链路，
        并保留 profile 学习副作用，供命令或工具直接下载用户选中的那一条结果。
        返回 ``{"outcome", "job", "error", "preflight", "sampled_chapter_count"}``。
        """
        started_at = time.monotonic()
        source_id = str(candidate.get("source_id") or "").strip()
        book_url = str(candidate.get("book_url") or "").strip()
        book_name = str(candidate.get("title") or "").strip()
        if not source_id or not book_url:
            return {
                "outcome": "invalid_candidate",
                "job": {},
                "error": "候选缺少 source_id 或 book_url，无法精确下载",
                "source_id": source_id,
                "book_url": book_url,
            }

        try:
            try:
                preflight = self.source_download_service.preflight_book(
                    source_id,
                    book_url,
                    book_name,
                    rule_context=dict(candidate.get("_rule_vars") or {}),
                )
            except TypeError as exc:
                if "rule_context" not in str(exc):
                    raise
                preflight = self.source_download_service.preflight_book(
                    source_id,
                    book_url,
                    book_name,
                )
        except Exception as exc:
            return {
                "outcome": "preflight_failed",
                "job": {},
                "error": str(exc),
                "source_id": source_id,
                "book_url": book_url,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000.0, 3),
            }

        try:
            sample = self.source_download_service.sample_book(preflight)
            self._update_profile_after_sample(source_id, preflight, sample=sample)
        except Exception as exc:
            self._update_profile_after_sample(source_id, preflight, error=str(exc))
            return {
                "outcome": "sample_failed",
                "job": {},
                "error": str(exc),
                "source_id": source_id,
                "book_url": book_url,
                "preflight": preflight,
            }

        validated_plan = dict(preflight)
        validated_plan.update(sample)
        try:
            job = self.source_download_service.create_job_from_plan(
                validated_plan, output_filename
            )
        except Exception as exc:
            return {
                "outcome": "job_create_failed",
                "job": {},
                "error": str(exc),
                "source_id": source_id,
                "book_url": book_url,
                "preflight": validated_plan,
                "sampled_chapter_count": int(
                    sample.get("sampled_chapter_count", 0) or 0
                ),
            }

        return {
            "outcome": "started",
            "job": job,
            "error": "",
            "source_id": source_id,
            "book_url": book_url,
            "preflight": validated_plan,
            "toc_count": int(validated_plan.get("toc_count", 0) or 0),
            "sampled_chapter_count": int(sample.get("sampled_chapter_count", 0) or 0),
            "elapsed_ms": round((time.monotonic() - started_at) * 1000.0, 3),
        }

    def download_candidate_group(
        self,
        candidate_group: Dict[str, Any],
        attempt_limit: int = 0,
        output_filename: str = "",
    ) -> Dict[str, Any]:
        """从同一本书的多个书源候选中择一下载，只创建一个正式任务。"""
        group = dict(candidate_group or {})
        candidates = [dict(item) for item in list(group.get("candidates") or [])]
        if not candidates and group.get("source_id") and group.get("book_url"):
            candidates = [group]
        skipped_candidates = [
            dict(item) for item in list(group.get("skipped_candidates") or [])
        ]
        effective_attempt_limit = max(
            1,
            int(attempt_limit or 0) or int(self.config.default_attempt_limit),
        )
        resolution = self._build_group_resolution(
            group,
            candidates,
            skipped_candidates,
            effective_attempt_limit,
        )
        if not candidates:
            return self._build_result(
                "no_attemptable_candidates",
                resolution,
                effective_attempt_limit,
                [],
                {},
                {},
                "这个聚合结果没有可自动下载的书源候选",
            )
        selected_candidates = candidates[:effective_attempt_limit]
        return self._download_candidate_group_pool(
            resolution,
            selected_candidates,
            effective_attempt_limit,
            output_filename,
        )

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
            book_name = str(
                candidate.get("title") or resolution.get("keyword") or ""
            ).strip()
            try:
                try:
                    preflight = self.source_download_service.preflight_book(
                        source_id,
                        book_url,
                        book_name,
                        rule_context=dict(candidate.get("_rule_vars") or {}),
                    )
                except TypeError as exc:
                    if "rule_context" not in str(exc):
                        raise
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
                        "source_name": str(
                            candidate.get("source_name") or source_id
                        ).strip(),
                        "title": book_name,
                        "author": str(candidate.get("author") or "").strip(),
                        "book_url": book_url,
                        "outcome": "preflight_failed",
                        "error": str(exc),
                        "elapsed_ms": round(
                            (time.monotonic() - started_at) * 1000.0, 3
                        ),
                    }
                )
                continue

            preflight_elapsed_ms = round((time.monotonic() - started_at) * 1000.0, 3)
            sample = {}
            try:
                sample = self.source_download_service.sample_book(preflight)
                self._update_profile_after_sample(source_id, preflight, sample=sample)
            except Exception as exc:
                attempts.append(
                    {
                        "attempt_index": attempt_index,
                        "source_id": source_id,
                        "source_name": str(
                            candidate.get("source_name") or source_id
                        ).strip(),
                        "title": book_name,
                        "author": str(candidate.get("author") or "").strip(),
                        "book_url": book_url,
                        "outcome": "sample_failed",
                        "error": str(exc),
                        "elapsed_ms": round(
                            (time.monotonic() - started_at) * 1000.0, 3
                        ),
                        "preflight": preflight,
                    }
                )
                self._update_profile_after_sample(source_id, preflight, error=str(exc))
                continue

            validated_plan = dict(preflight)
            validated_plan.update(sample)
            try:
                job = self.source_download_service.create_job_from_plan(
                    validated_plan, output_filename
                )
            except Exception as exc:
                attempts.append(
                    {
                        "attempt_index": attempt_index,
                        "source_id": source_id,
                        "source_name": str(
                            candidate.get("source_name") or source_id
                        ).strip(),
                        "title": book_name,
                        "author": str(candidate.get("author") or "").strip(),
                        "book_url": book_url,
                        "outcome": "job_create_failed",
                        "error": str(exc),
                        "elapsed_ms": preflight_elapsed_ms,
                        "preflight": validated_plan,
                        "sampled_chapter_count": int(
                            sample.get("sampled_chapter_count", 0) or 0
                        ),
                    }
                )
                continue

            attempts.append(
                {
                    "attempt_index": attempt_index,
                    "source_id": source_id,
                    "source_name": str(
                        candidate.get("source_name") or source_id
                    ).strip(),
                    "title": book_name,
                    "author": str(candidate.get("author") or "").strip(),
                    "book_url": book_url,
                    "outcome": "started",
                    "elapsed_ms": preflight_elapsed_ms,
                    "preflight": validated_plan,
                    "job_id": job.get("job_id", ""),
                    "toc_count": int(validated_plan.get("toc_count", 0) or 0),
                    "sampled_chapter_count": int(
                        sample.get("sampled_chapter_count", 0) or 0
                    ),
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

        failure_reason = ""
        if attempts:
            failure_reason = str(attempts[-1].get("error") or "").strip()
        status = "all_preflight_failed"
        if any(
            str(item.get("outcome") or "").strip()
            in {"sample_failed", "job_create_failed"}
            for item in attempts
        ):
            status = "all_candidates_failed"
        return self._build_result(
            status,
            resolution,
            effective_attempt_limit,
            attempts,
            {},
            {},
            failure_reason or "候选书源都未通过自动化验证",
        )

    def _download_candidate_group_pool(
        self,
        resolution: dict[str, Any],
        candidates: list[dict[str, Any]],
        attempt_limit: int,
        output_filename: str,
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        worker_count = min(
            max(1, int(self.config.group_preflight_workers)),
            max(1, len(candidates)),
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
        future_map: dict[concurrent.futures.Future, dict[str, Any]] = {}
        try:
            for attempt_index, candidate in enumerate(candidates):
                future = executor.submit(
                    self._preflight_candidate_for_group,
                    candidate,
                    attempt_index,
                )
                future_map[future] = candidate

            for future in concurrent.futures.as_completed(future_map):
                candidate, attempt, preflight = future.result()
                if attempt.get("outcome") == "preflight_failed":
                    attempts.append(attempt)
                    continue

                source_id = str(candidate.get("source_id") or "").strip()
                sample_started_at = time.monotonic()
                sample = {}
                try:
                    sample = self.source_download_service.sample_book(preflight)
                    self._update_profile_after_sample(
                        source_id, preflight, sample=sample
                    )
                except Exception as exc:
                    attempt.update(
                        {
                            "outcome": "sample_failed",
                            "error": str(exc),
                            "elapsed_ms": round(
                                (time.monotonic() - sample_started_at) * 1000.0
                                + float(attempt.get("elapsed_ms", 0.0) or 0.0),
                                3,
                            ),
                            "preflight": preflight,
                        }
                    )
                    self._update_profile_after_sample(
                        source_id, preflight, error=str(exc)
                    )
                    attempts.append(attempt)
                    continue

                validated_plan = dict(preflight)
                validated_plan.update(sample)
                try:
                    job = self.source_download_service.create_job_from_plan(
                        validated_plan, output_filename
                    )
                except Exception as exc:
                    attempt.update(
                        {
                            "outcome": "job_create_failed",
                            "error": str(exc),
                            "preflight": validated_plan,
                            "sampled_chapter_count": int(
                                sample.get("sampled_chapter_count", 0) or 0
                            ),
                        }
                    )
                    attempts.append(attempt)
                    continue

                attempt.update(
                    {
                        "outcome": "started",
                        "error": "",
                        "preflight": validated_plan,
                        "job_id": job.get("job_id", ""),
                        "toc_count": int(validated_plan.get("toc_count", 0) or 0),
                        "sampled_chapter_count": int(
                            sample.get("sampled_chapter_count", 0) or 0
                        ),
                    }
                )
                attempts.append(attempt)
                for pending in future_map:
                    if pending is not future:
                        pending.cancel()
                return self._build_result(
                    "started",
                    resolution,
                    attempt_limit,
                    attempts,
                    dict(candidate),
                    job,
                    "",
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        failure_reason = ""
        if attempts:
            failure_reason = str(attempts[-1].get("error") or "").strip()
        status = "all_preflight_failed"
        if any(
            str(item.get("outcome") or "").strip()
            in {"sample_failed", "job_create_failed"}
            for item in attempts
        ):
            status = "all_candidates_failed"
        return self._build_result(
            status,
            resolution,
            attempt_limit,
            attempts,
            {},
            {},
            failure_reason or "候选书源都未通过自动化验证",
        )

    def _preflight_candidate_for_group(
        self,
        candidate: dict[str, Any],
        attempt_index: int,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        started_at = time.monotonic()
        source_id = str(candidate.get("source_id") or "").strip()
        book_url = str(candidate.get("book_url") or "").strip()
        book_name = str(candidate.get("title") or "").strip()
        attempt = {
            "attempt_index": attempt_index,
            "source_id": source_id,
            "source_name": str(candidate.get("source_name") or source_id).strip(),
            "title": book_name,
            "author": str(candidate.get("author") or "").strip(),
            "book_url": book_url,
            "outcome": "preflight_ready",
            "error": "",
        }
        try:
            try:
                preflight = self.source_download_service.preflight_book(
                    source_id,
                    book_url,
                    book_name,
                    rule_context=dict(candidate.get("_rule_vars") or {}),
                )
            except TypeError as exc:
                if "rule_context" not in str(exc):
                    raise
                preflight = self.source_download_service.preflight_book(
                    source_id,
                    book_url,
                    book_name,
                )
        except Exception as exc:
            attempt.update(
                {
                    "outcome": "preflight_failed",
                    "error": str(exc),
                    "elapsed_ms": round((time.monotonic() - started_at) * 1000.0, 3),
                }
            )
            return dict(candidate), attempt, {}

        attempt["elapsed_ms"] = round((time.monotonic() - started_at) * 1000.0, 3)
        return dict(candidate), attempt, preflight

    def _build_group_resolution(
        self,
        group: dict[str, Any],
        candidates: list[dict[str, Any]],
        skipped_candidates: list[dict[str, Any]],
        attempt_limit: int,
    ) -> dict[str, Any]:
        title = str(group.get("title") or "").strip()
        author = str(group.get("author") or "").strip()
        return {
            "keyword": title,
            "author": author,
            "source_ids": [
                str(item.get("source_id") or "").strip()
                for item in candidates
                if str(item.get("source_id") or "").strip()
            ],
            "include_disabled": False,
            "limit": attempt_limit,
            "search_result": {
                "result_count": len(candidates) + len(skipped_candidates),
                "candidate_sources": int(group.get("source_count", 0) or 0),
                "successful_sources": int(group.get("source_count", 0) or 0),
            },
            "candidate_count": len(candidates),
            "skipped_candidate_count": len(skipped_candidates),
            "candidate_group": group,
            "candidates": candidates,
            "skipped_candidates": skipped_candidates,
        }

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
            "skipped_candidate_count": int(
                resolution.get("skipped_candidate_count", 0) or 0
            ),
            "search_result": search_result,
            "candidate_group": dict(resolution.get("candidate_group") or {}),
            "candidates": list(resolution.get("candidates") or []),
            "skipped_candidates": list(resolution.get("skipped_candidates") or []),
            "attempt_limit": attempt_limit,
            "attempted_count": len(attempts),
            "attempts": attempts,
            "chosen": selected,
            "selected": selected,
            "job_info": job,
            "job": job,
        }

    def _update_profile_after_sample(
        self,
        source_id: str,
        preflight: dict[str, Any],
        sample: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        if self.source_profile_service is None:
            return
        normalized_source_id = str(source_id or "").strip()
        if not normalized_source_id:
            return
        patch = {
            "download_strategy": {
                "last_sample_state": "failed" if error else "healthy",
                "last_sample_book_url": str(preflight.get("book_url") or "").strip(),
                "last_sample_book_name": str(preflight.get("book_name") or "").strip(),
                "last_sampled_chapter_count": int(
                    (sample or {}).get("sampled_chapter_count", 0) or 0
                ),
                "last_sample_error": str(error or "").strip(),
            }
        }
        try:
            self.source_profile_service.update(normalized_source_id, patch)
        except Exception:
            pass
