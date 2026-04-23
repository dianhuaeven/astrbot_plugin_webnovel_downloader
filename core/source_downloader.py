from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .download_manager import ExtractionRules, NovelDownloadManager
from .rule_engine import RuleEngine, RuleEngineError
from .source_registry import SourceRegistry


@dataclass
class SourceDownloadConfig:
    max_workers: int = 3
    sample_chapters: int = 1
    sample_min_chars: int = 1
    stop_after_consecutive_failures: int = 6
    stop_after_same_error: int = 3


class SourceDownloadService:
    def __init__(
        self,
        registry: SourceRegistry,
        engine: RuleEngine,
        manager: NovelDownloadManager,
        config: Optional[SourceDownloadConfig] = None,
        source_health_store: Any = None,
        source_profile_service: Any = None,
    ):
        self.registry = registry
        self.engine = engine
        self.manager = manager
        self.config = config or SourceDownloadConfig()
        self.source_health_store = source_health_store
        self.source_profile_service = source_profile_service

    def create_book_job(
        self,
        source_id: str,
        book_url: str,
        book_name: str = "",
        output_filename: str = "",
    ) -> Dict[str, Any]:
        plan = self.preflight_book(source_id, book_url, book_name)
        return self.create_job_from_plan(plan, output_filename)

    def preflight_book(
        self,
        source_id: str,
        book_url: str,
        book_name: str = "",
        rule_context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        summary = self._get_supported_download_summary(source_id)
        source = self.registry.load_normalized_source(source_id)
        plan = self._build_book_download_plan(source, book_url, book_name, rule_context)
        toc = list(plan.get("toc") or [])
        return {
            "source_id": source_id,
            "source_name": summary.get("name") or source_id,
            "source_url": summary.get("source_url", ""),
            "book_url": plan.get("book_url", book_url),
            "toc_url": plan.get("toc_url", ""),
            "book_name": plan.get("book_name") or book_name or "未命名小说",
            "author": plan.get("author", ""),
            "intro": plan.get("intro", ""),
            "toc": toc,
            "toc_count": len(toc),
            "_rule_vars": dict(plan.get("_rule_vars") or {}),
        }

    def create_job_from_plan(
        self,
        plan: Dict[str, Any],
        output_filename: str = "",
    ) -> Dict[str, Any]:
        source_id = str(plan.get("source_id") or "").strip()
        job = self.manager.create_job(
            str(plan.get("book_name") or "").strip() or "未命名小说",
            list(plan.get("toc") or []),
            ExtractionRules(content_regex=r"(?s)(.*)"),
            output_filename,
            str(plan.get("book_url") or "").strip(),
            metadata={
                "download_mode": "rule_based",
                "source_id": source_id,
                "source_name": str(plan.get("source_name") or "").strip(),
                "book_url": str(plan.get("book_url") or "").strip(),
                "toc_url": str(plan.get("toc_url") or "").strip(),
                "author": plan.get("author", ""),
                "intro": plan.get("intro", ""),
                "sampled_chapter_count": int(plan.get("sampled_chapter_count", 0) or 0),
                "rule_vars": dict(plan.get("_rule_vars") or {}),
            },
        )
        return {
            "job_id": job["job_id"],
            "created": job["created"],
            "source_id": source_id,
            "source_name": str(plan.get("source_name") or "").strip(),
            "book_name": str(plan.get("book_name") or "").strip(),
            "book_url": str(plan.get("book_url") or "").strip(),
            "toc_url": str(plan.get("toc_url") or "").strip(),
            "toc_count": int(plan.get("toc_count", len(plan.get("toc") or [])) or 0),
            "sampled_chapter_count": int(plan.get("sampled_chapter_count", 0) or 0),
            "preflight": {
                "source_id": source_id,
                "source_name": str(plan.get("source_name") or "").strip(),
                "book_name": str(plan.get("book_name") or "").strip(),
                "book_url": str(plan.get("book_url") or "").strip(),
                "toc_url": str(plan.get("toc_url") or "").strip(),
                "toc_count": int(
                    plan.get("toc_count", len(plan.get("toc") or [])) or 0
                ),
                "author": str(plan.get("author") or "").strip(),
                "intro": str(plan.get("intro") or "").strip(),
            },
            "sampled_chapters": list(plan.get("sampled_chapters") or []),
            "status": self.manager.get_status(job["job_id"]),
        }

    def sample_book(
        self,
        plan: Dict[str, Any],
        chapter_count: int | None = None,
        min_content_chars: int | None = None,
    ) -> Dict[str, Any]:
        source_id = str(plan.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("preflight 结果缺少 source_id，无法做正文抽样")
        toc = list(plan.get("toc") or [])
        if not toc:
            raise ValueError("preflight 结果缺少目录，无法做正文抽样")

        source = self.registry.load_normalized_source(source_id)
        sample_size = max(
            1, int(chapter_count or 0) or int(self.config.sample_chapters)
        )
        min_chars = max(
            1, int(min_content_chars or 0) or int(self.config.sample_min_chars)
        )
        chapters = self._select_sample_chapters(toc, sample_size)
        sampled_chapters: list[dict[str, Any]] = []
        sample_errors: list[dict[str, Any]] = []
        last_error: Exception | None = None

        for chapter in chapters:
            started_at = time.monotonic()
            try:
                payload = self._download_one_chapter(source, chapter)
                content = str(payload.get("content") or "").strip()
                if len(content) < min_chars:
                    raise RuleEngineError(
                        "正文抽样内容过短: {size} < {minimum}".format(
                            size=len(content),
                            minimum=min_chars,
                        )
                    )
                sampled_chapters.append(
                    {
                        "index": int(chapter.get("index", 0) or 0),
                        "title": str(
                            payload.get("title") or chapter.get("title") or ""
                        ).strip(),
                        "url": str(chapter.get("url") or "").strip(),
                        "content_chars": len(content),
                        "elapsed_ms": round(
                            (time.monotonic() - started_at) * 1000.0, 3
                        ),
                    }
                )
            except Exception as exc:
                last_error = exc
                sample_errors.append(
                    {
                        "index": int(chapter.get("index", 0) or 0),
                        "title": str(chapter.get("title") or "").strip(),
                        "url": str(chapter.get("url") or "").strip(),
                        "error": str(exc),
                        "elapsed_ms": round(
                            (time.monotonic() - started_at) * 1000.0, 3
                        ),
                    }
                )

        if not sampled_chapters:
            if last_error is None:
                raise ValueError("正文抽样失败：未选到可抓取章节")
            raise RuleEngineError("正文抽样失败：{error}".format(error=last_error))

        return {
            "sampled_chapter_count": len(sampled_chapters),
            "requested_sample_count": len(chapters),
            "sampled_chapters": sampled_chapters,
            "sample_errors": sample_errors,
            "min_content_chars": min_chars,
        }

    def resume_book_job(
        self, job_id: str, auto_assemble: bool = True
    ) -> Dict[str, Any]:
        started_at = time.monotonic()
        manifest = self.manager.load_manifest(job_id)
        metadata = manifest.get("metadata") or {}
        if metadata.get("download_mode") != "rule_based":
            raise ValueError("任务不是书源规则下载任务，不能用 rule_based 恢复")

        source_id = metadata.get("source_id")
        if not source_id:
            raise ValueError("任务缺少 source_id，无法恢复")

        source = self.registry.load_normalized_source(source_id)
        failure_status: dict[str, Any] | None = None
        missing = self.manager.get_missing_chapters(job_id)
        missing = self._hydrate_missing_rule_contexts(manifest, source_id, missing)
        if not missing:
            if auto_assemble:
                status = self.manager.assemble(
                    job_id,
                    self.manager.config.cleanup_journal_after_assemble,
                )
            else:
                status = self.manager.get_status(job_id)
            self._record_download_outcome(
                manifest,
                status,
                elapsed_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
            )
            return status

        self.manager.record_state(job_id, "downloading", missing_count=len(missing))
        download_result = self._download_missing_chapters(source, job_id, missing)
        errors = int(download_result.get("error_count", 0) or 0)
        if errors:
            state_extra = {
                "error_count": errors,
            }
            stop_reason = str(download_result.get("stop_reason") or "").strip()
            if stop_reason:
                state_extra["stop_reason"] = stop_reason
            self.manager.record_state(job_id, "failed", **state_extra)
            failure_status = self.manager.get_status(job_id)
            self._record_download_outcome(
                manifest,
                failure_status,
                elapsed_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
            )
            return failure_status

        self.manager.record_state(job_id, "downloaded")
        if auto_assemble:
            status = self.manager.assemble(
                job_id,
                self.manager.config.cleanup_journal_after_assemble,
            )
        else:
            status = self.manager.get_status(job_id)
        self._record_download_outcome(
            manifest,
            status,
            elapsed_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
        )
        return status

    def _download_missing_chapters(
        self,
        source: Dict[str, Any],
        job_id: str,
        missing: list[Dict[str, Any]],
    ) -> Dict[str, Any]:
        error_count = 0
        max_workers = min(max(1, self.config.max_workers), max(1, len(missing)))
        consecutive_failures = 0
        same_error_streak = 0
        last_error_text = ""
        stop_reason = ""
        remaining = iter(missing)

        def _submit_next(
            executor: concurrent.futures.ThreadPoolExecutor,
            future_map: Dict[concurrent.futures.Future, Dict[str, Any]],
        ) -> bool:
            try:
                chapter = next(remaining)
            except StopIteration:
                return False
            future_map[executor.submit(self._download_one_chapter, source, chapter)] = (
                chapter
            )
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map: Dict[concurrent.futures.Future, Dict[str, Any]] = {}
            for _ in range(max_workers):
                if not _submit_next(executor, future_map):
                    break

            while future_map:
                done, _pending = concurrent.futures.wait(
                    list(future_map.keys()),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    chapter = future_map.pop(future)
                    if future.cancelled():
                        continue
                    try:
                        result = future.result()
                    except Exception as exc:
                        error_text = str(exc)
                        error_count += 1
                        consecutive_failures += 1
                        if error_text == last_error_text:
                            same_error_streak += 1
                        else:
                            last_error_text = error_text
                            same_error_streak = 1
                        self.manager.append_download_error(
                            job_id,
                            chapter["index"],
                            chapter["title"],
                            chapter["url"],
                            error_text,
                            1,
                        )
                        if not stop_reason:
                            stop_reason = self._build_failure_stop_reason(
                                error_text,
                                consecutive_failures,
                                same_error_streak,
                            )
                        continue

                    consecutive_failures = 0
                    same_error_streak = 0
                    last_error_text = ""
                    self.manager.append_downloaded_chapter(
                        job_id,
                        chapter["index"],
                        result["title"],
                        chapter["url"],
                        result["content"],
                        result.get("encoding", ""),
                        1,
                    )

                if stop_reason:
                    for pending_future in list(future_map.keys()):
                        pending_future.cancel()
                    continue

                while len(future_map) < max_workers:
                    if not _submit_next(executor, future_map):
                        break
        return {
            "error_count": error_count,
            "stop_reason": stop_reason,
        }

    def _hydrate_missing_rule_contexts(
        self,
        manifest: Dict[str, Any],
        source_id: str,
        missing: list[Dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        chapters = [dict(chapter) for chapter in list(missing or [])]
        if not chapters:
            return chapters
        if all(dict(chapter.get("_rule_vars") or {}) for chapter in chapters):
            return chapters

        metadata = dict(manifest.get("metadata") or {})
        book_url = str(metadata.get("book_url") or "").strip()
        if not book_url:
            return chapters

        try:
            preflight = self.preflight_book(
                source_id,
                book_url,
                str(manifest.get("book_name") or "").strip(),
                rule_context=dict(metadata.get("rule_vars") or {}),
            )
        except Exception:
            return chapters

        toc = list(preflight.get("toc") or [])
        rule_vars_by_url = {
            str(item.get("url") or "").strip(): dict(item.get("_rule_vars") or {})
            for item in toc
            if str(item.get("url") or "").strip()
        }
        rule_vars_by_index = {
            int(item.get("index", -1) or -1): dict(item.get("_rule_vars") or {})
            for item in toc
            if int(item.get("index", -1) or -1) >= 0
        }
        for chapter in chapters:
            existing_rule_vars = dict(chapter.get("_rule_vars") or {})
            if existing_rule_vars:
                continue
            chapter_url = str(chapter.get("url") or "").strip()
            restored_rule_vars = dict(rule_vars_by_url.get(chapter_url) or {})
            if not restored_rule_vars:
                chapter_index = int(chapter.get("index", -1) or -1)
                restored_rule_vars = dict(rule_vars_by_index.get(chapter_index) or {})
            if restored_rule_vars:
                chapter["_rule_vars"] = restored_rule_vars
        return chapters

    def _build_failure_stop_reason(
        self,
        error_text: str,
        consecutive_failures: int,
        same_error_streak: int,
    ) -> str:
        repeated_error_limit = max(2, int(self.config.stop_after_same_error or 0))
        if error_text and same_error_streak >= repeated_error_limit:
            return (
                "连续 {count} 次出现相同下载错误，已停止继续派发新章节: {error}".format(
                    count=same_error_streak,
                    error=error_text,
                )
            )
        consecutive_limit = max(
            repeated_error_limit,
            int(self.config.stop_after_consecutive_failures or 0),
        )
        if consecutive_failures >= consecutive_limit:
            return "连续 {count} 个章节下载失败，已停止继续派发新章节: {error}".format(
                count=consecutive_failures,
                error=error_text,
            )
        return ""

    def _download_one_chapter(
        self, source: Dict[str, Any], chapter: Dict[str, Any]
    ) -> Dict[str, str]:
        try:
            return self._fetch_chapter_content(source, chapter)
        except RuleEngineError:
            raise
        except Exception as exc:
            raise RuleEngineError(str(exc))

    def _build_book_download_plan(
        self,
        source: Dict[str, Any],
        book_url: str,
        book_name: str,
        rule_context: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        try:
            return self.engine.build_book_download_plan(
                source,
                book_url,
                book_name,
                rule_context=rule_context,
            )
        except TypeError as exc:
            if "rule_context" not in str(exc):
                raise
            return self.engine.build_book_download_plan(source, book_url, book_name)

    def _fetch_chapter_content(
        self,
        source: Dict[str, Any],
        chapter: Dict[str, Any],
    ) -> Dict[str, str]:
        try:
            return self.engine.fetch_chapter_content(
                source,
                chapter["url"],
                chapter["title"],
                rule_context=dict(chapter.get("_rule_vars") or {}),
            )
        except TypeError as exc:
            if "rule_context" not in str(exc):
                raise
            return self.engine.fetch_chapter_content(
                source,
                chapter["url"],
                chapter["title"],
            )

    def _select_sample_chapters(
        self,
        toc: list[Dict[str, Any]],
        sample_size: int,
    ) -> list[Dict[str, Any]]:
        chapters = [
            dict(chapter) for chapter in toc if str(chapter.get("url") or "").strip()
        ]
        if not chapters:
            return []
        if sample_size <= 1 or len(chapters) == 1:
            return [chapters[0]]
        if sample_size >= len(chapters):
            return chapters

        selected: list[Dict[str, Any]] = []
        seen_urls: set[str] = set()
        candidate_indexes = [0, len(chapters) // 2, len(chapters) - 1]
        for index in candidate_indexes:
            chapter = chapters[index]
            chapter_url = str(chapter.get("url") or "").strip()
            if chapter_url in seen_urls:
                continue
            seen_urls.add(chapter_url)
            selected.append(chapter)
            if len(selected) >= sample_size:
                return selected
        for chapter in chapters:
            chapter_url = str(chapter.get("url") or "").strip()
            if chapter_url in seen_urls:
                continue
            seen_urls.add(chapter_url)
            selected.append(chapter)
            if len(selected) >= sample_size:
                break
        return selected

    def _record_download_outcome(
        self,
        manifest: Dict[str, Any],
        status: Dict[str, Any],
        elapsed_ms: float,
    ) -> None:
        source_id = str((manifest.get("metadata") or {}).get("source_id") or "").strip()
        if not source_id:
            return
        metadata = manifest.get("metadata") or {}
        summary_metadata = {
            "sample_book_name": str(manifest.get("book_name") or "").strip(),
            "sample_book_url": str(metadata.get("book_url") or "").strip(),
            "toc_count": len(list(manifest.get("chapters") or [])),
            "completed_chapters": int(status.get("completed_chapters", 0) or 0),
            "failed_chapters": int(status.get("failed_chapters", 0) or 0),
        }
        state = str(status.get("state") or "").strip()
        failed_chapters = int(status.get("failed_chapters", 0) or 0)
        if self.source_health_store is not None:
            if state in {"downloaded", "assembled"} and failed_chapters == 0:
                self.source_health_store.record_success(
                    source_id,
                    "download",
                    elapsed_ms=elapsed_ms,
                    summary="正文下载成功",
                    metadata=summary_metadata,
                )
            else:
                latest_errors = list(status.get("latest_errors") or [])
                error_summary = ""
                if latest_errors:
                    error_summary = str(latest_errors[0].get("error") or "").strip()
                self.source_health_store.record_failure(
                    source_id,
                    "download",
                    elapsed_ms=elapsed_ms,
                    error_code="download_failed",
                    error_summary=error_summary or "正文下载未完成",
                    metadata=summary_metadata,
                )
        if self.source_profile_service is not None:
            try:
                self.source_profile_service.update(
                    source_id,
                    {
                        "download_strategy": {
                            "last_download_state": state or "unknown",
                            "last_completed_chapters": summary_metadata[
                                "completed_chapters"
                            ],
                            "last_failed_chapters": failed_chapters,
                        }
                    },
                )
            except Exception:
                pass

    def _get_supported_download_summary(self, source_id: str) -> Dict[str, Any]:
        summary = self.registry.get_source_summary(source_id)
        if summary.get("supports_download"):
            if self.source_health_store is not None:
                try:
                    health = dict(
                        self.source_health_store.get_source_health(source_id) or {}
                    )
                except Exception:
                    health = {}
                for stage in ("preflight", "download"):
                    stage_entry = dict(health.get(stage) or {})
                    stage_state = str(stage_entry.get("state", "unknown") or "unknown")
                    if stage_state != "unsupported":
                        continue
                    issues = str(
                        stage_entry.get("note")
                        or stage_entry.get("last_error_summary")
                        or ""
                    ).strip()
                    raise ValueError(
                        "书源 {name} 当前不支持 TXT 下载：{issues}".format(
                            name=summary.get("name") or source_id,
                            issues=issues or "当前书源不支持 route A TXT 下载",
                        )
                    )
            return summary
        issues = (
            "；".join(summary.get("issues") or []) or "当前书源不支持 route A TXT 下载"
        )
        raise ValueError(
            "书源 {name} 当前不支持 TXT 下载：{issues}".format(
                name=summary.get("name") or source_id,
                issues=issues,
            )
        )
