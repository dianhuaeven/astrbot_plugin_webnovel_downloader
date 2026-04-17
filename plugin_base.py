from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api.star import Star
from astrbot.core.star.star_tools import StarTools

from .core.download_manager import ExtractionRules
from .plugin_renderer import ToolRenderConfig, ToolResultRenderer
from .plugin_runtime import build_plugin_runtime
from .search_cache import SearchCacheStore
from .plugin_support import logger, run_blocking
from .text_loader import load_text_argument


PLUGIN_NAME = "astrbot_plugin_webnovel_downloader"


class JsonlNovelDownloaderPluginBase(Star):
    def __init__(self, context: Any, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.plugin_data_dir = self._resolve_plugin_data_dir()
        self.reports_dir = self.plugin_data_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.search_cache = SearchCacheStore(self.plugin_data_dir)

        self.max_preview_fetch_chars = max(
            300, int(self.config.get("max_preview_fetch_chars", 1800))
        )
        runtime = build_plugin_runtime(self.plugin_data_dir, self.config)
        self.manager = runtime.manager
        self.source_registry = runtime.source_registry
        self.clean_rule_store = runtime.clean_rule_store
        self.source_health_store = runtime.source_health_store
        self.source_probe_service = runtime.source_probe_service
        self.search_service = runtime.search_service
        self.book_resolution_service = runtime.book_resolution_service
        self.source_download_service = runtime.source_download_service
        self.download_orchestrator = runtime.download_orchestrator
        self.auto_probe_on_import = bool(self.config.get("auto_probe_on_import", True))
        self.renderer = ToolResultRenderer(
            self.reports_dir,
            self.source_registry,
            self.manager,
            ToolRenderConfig(
                max_tool_response_chars=max(
                    800, int(self.config.get("max_tool_response_chars", 2800))
                ),
                max_tool_preview_items=max(
                    1, int(self.config.get("max_tool_preview_items", 8))
                ),
                max_tool_preview_text=max(
                    60, int(self.config.get("max_tool_preview_text", 180))
                ),
            ),
        )
        self.max_tool_response_chars = self.renderer.config.max_tool_response_chars
        self.max_tool_preview_items = self.renderer.config.max_tool_preview_items
        self.max_tool_preview_text = self.renderer.config.max_tool_preview_text
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._bootstrap_state_path = self.plugin_data_dir / "bootstrap_state.json"
        self._bootstrap_thread: threading.Thread | None = None
        self._bootstrap_done = threading.Event()
        self._bootstrap_done.set()
        self._schedule_bootstrap_config_sources()
        logger.info("网文下载器初始化完成")

    def _resolve_plugin_data_dir(self) -> Path:
        plugin_name = str(getattr(self, "name", "") or PLUGIN_NAME).strip() or PLUGIN_NAME
        get_data_dir = getattr(StarTools, "get_data_dir", None)
        if callable(get_data_dir):
            try:
                return Path(get_data_dir(plugin_name))
            except TypeError:
                return Path(get_data_dir())
            except ValueError:
                logger.warning(
                    "StarTools.get_data_dir 无法自动解析插件名称，回退到固定插件名: %s",
                    plugin_name,
                )

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir.resolve()

    async def handle_novel_fetch_preview(
        self,
        url: str,
        encoding: str = "",
        max_chars: str = "",
    ) -> str:
        limit = min(
            self._parse_optional_int(max_chars) or self.max_preview_fetch_chars,
            self.max_preview_fetch_chars,
        )
        preview = await run_blocking(
            self.manager.fetch_preview,
            url,
            encoding,
            limit,
        )
        preview["html_preview"] = self.renderer.truncate_text(preview.get("html_preview", ""), limit)
        preview["text_preview"] = self.renderer.truncate_text(preview.get("text_preview", ""), limit)
        preview["applied_max_chars"] = limit
        return await run_blocking(self.renderer.to_json_text, preview)

    async def handle_novel_import_sources(self, source_json: str) -> str:
        source_text = await self._load_text_argument(source_json)
        result = await run_blocking(
            self.source_registry.import_sources_from_text,
            source_text,
        )
        probe_result = await run_blocking(self._queue_probe_for_import_result, result)
        result = {
            **dict(result),
            **probe_result,
        }
        return await run_blocking(self.renderer.render_import_summary, result)

    async def handle_novel_import_clean_rules(
        self,
        repo_json: str,
        repo_name: str = "",
    ) -> str:
        repo_text = await self._load_text_argument(repo_json)
        record = await run_blocking(
            self.clean_rule_store.import_rules_from_text,
            repo_text,
            repo_name,
            repo_json,
        )
        return await run_blocking(self.renderer.render_clean_rule_import_summary, record)

    async def handle_novel_list_clean_rules(self, limit: str = "", offset: str = "") -> str:
        repos = await run_blocking(self.clean_rule_store.list_repositories)
        return await run_blocking(
            self.renderer.render_clean_rule_list_summary,
            repos,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    async def handle_novel_list_sources(
        self,
        enabled_only: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        enabled_only_value = self._parse_bool(enabled_only, False)
        limit_value = self._parse_optional_int(limit) or self.max_tool_preview_items
        offset_value = self._parse_non_negative_int(offset, 0)
        result = await run_blocking(
            self.source_registry.list_sources,
            enabled_only_value,
        )
        result = await run_blocking(self.source_health_store.enrich_sources, result)
        return await run_blocking(
            self.renderer.render_sources_summary,
            result,
            enabled_only_value,
            limit_value,
            offset_value,
        )

    async def handle_novel_refresh_sources(
        self,
        source_ids_json: str = "",
        include_disabled: str = "",
    ) -> str:
        requested_source_ids = self._parse_string_list(source_ids_json)
        include_disabled_value = self._parse_bool(include_disabled, False)
        selected_sources = await run_blocking(
            self.source_registry.load_enabled_source_summaries,
            requested_source_ids or None,
            include_disabled_value,
        )
        selected_source_ids = [
            str(item.get("source_id") or "").strip()
            for item in selected_sources
            if str(item.get("source_id") or "").strip()
        ]
        selected_source_id_set = set(selected_source_ids)
        ignored_source_ids = [
            source_id
            for source_id in requested_source_ids
            if source_id not in selected_source_id_set
        ]
        queued_result = {
            "queued_count": 0,
            "queue_size": 0,
        }
        if selected_source_ids:
            queued_result = await run_blocking(
                self.source_probe_service.enqueue_sources,
                selected_source_ids,
            )
        probe_status = await run_blocking(self.source_probe_service.get_status)
        return await run_blocking(
            self.renderer.render_probe_enqueue_summary,
            selected_sources,
            list(requested_source_ids),
            ignored_source_ids,
            include_disabled_value,
            queued_result,
            probe_status,
        )

    async def handle_novel_enable_source(self, source_id: str, enabled: str = "true") -> str:
        result = await run_blocking(
            self.source_registry.set_enabled,
            source_id,
            self._parse_bool(enabled, True),
        )
        return await run_blocking(
            self.renderer.render_source_change_summary,
            "set_enabled",
            result,
        )

    async def handle_novel_remove_source(self, source_id: str) -> str:
        result = await run_blocking(self.source_registry.remove_source, source_id)
        return await run_blocking(
            self.renderer.render_source_change_summary,
            "removed",
            result,
        )

    async def handle_novel_search_books(
        self,
        keyword: str,
        source_ids_json: str = "",
        limit: str = "",
        include_disabled: str = "",
    ) -> str:
        source_ids = self._parse_string_list(source_ids_json)
        limit_value = self._parse_optional_int(limit) or 20
        include_disabled_value = self._parse_bool(include_disabled, False)
        result = await run_blocking(
            self.search_service.search,
            keyword,
            source_ids or None,
            limit_value,
            include_disabled_value,
        )
        cache_record = await run_blocking(
            self.search_cache.save_search,
            keyword,
            result,
            source_ids or None,
            include_disabled_value,
            limit_value,
        )
        return await run_blocking(
            self.renderer.render_search_summary_with_cache,
            result,
            cache_record,
        )

    async def handle_novel_list_searches(self, limit: str = "", offset: str = "") -> str:
        searches = await run_blocking(self.search_cache.list_searches)
        return await run_blocking(
            self.renderer.render_search_cache_list_summary,
            searches,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    async def handle_novel_get_search_results(
        self,
        search_id: str,
        limit: str = "",
        offset: str = "",
    ) -> str:
        payload = await run_blocking(self.search_cache.load_search, search_id)
        return await run_blocking(
            self.renderer.render_cached_search_results,
            payload,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    async def handle_novel_auto_download(
        self,
        keyword: str,
        author: str = "",
        source_ids_json: str = "",
        search_limit: str = "",
        attempt_limit: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
        include_disabled: str = "",
    ) -> str:
        source_ids = self._parse_string_list(source_ids_json)
        attempt_limit_value = max(1, self._parse_optional_int(attempt_limit) or 5)
        search_limit_value = max(
            attempt_limit_value,
            self._parse_optional_int(search_limit) or max(10, attempt_limit_value * 3),
        )
        include_disabled_value = self._parse_bool(include_disabled, False)
        orchestration = await run_blocking(
            self.download_orchestrator.auto_download,
            keyword,
            author,
            source_ids or None,
            search_limit_value,
            include_disabled_value,
            attempt_limit_value,
            output_filename,
        )
        cache_record = await run_blocking(
            self.search_cache.save_search,
            str(orchestration.get("keyword") or keyword).strip(),
            dict(orchestration.get("search_result") or {}),
            source_ids or None,
            include_disabled_value,
            search_limit_value,
        )
        await run_blocking(self._record_auto_download_attempts, orchestration)
        job_status = {}
        job_id = str(orchestration.get("job", {}).get("job_id") or "").strip()
        if job_id:
            await self._ensure_rule_job_running(job_id, self._parse_bool(auto_assemble, True))
            job_status = await run_blocking(self.manager.get_status, job_id)
        return await run_blocking(
            self.renderer.render_auto_download_summary,
            orchestration,
            cache_record,
            job_status,
        )

    async def handle_novel_download_book(
        self,
        source_id: str,
        book_url: str,
        book_name: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
    ) -> str:
        preflight = None
        try:
            preflight = await run_blocking(
                self.source_download_service.preflight_book,
                source_id,
                book_url,
                book_name,
            )
        except Exception as exc:
            await run_blocking(
                self._record_preflight_failure,
                source_id,
                book_url,
                book_name,
                str(exc),
            )
            raise

        await run_blocking(self._record_preflight_success, preflight)
        job_info = await run_blocking(
            self.source_download_service.create_job_from_plan,
            preflight,
            output_filename,
        )
        job_id = job_info["job_id"]
        await self._ensure_rule_job_running(job_id, self._parse_bool(auto_assemble, True))
        status_text = await self._render_job_status(job_id, created=job_info["created"])
        preflight_summary = "预检: source={source} toc={toc_count} book={book}".format(
            source=str(preflight.get("source_name") or source_id),
            toc_count=int(preflight.get("toc_count", 0) or 0),
            book=str(preflight.get("book_name") or book_name or ""),
        )
        return "{status}\n{summary}".format(status=status_text, summary=preflight_summary)

    async def handle_novel_download_search_result(
        self,
        search_id: str,
        result_index: str,
        output_filename: str = "",
        auto_assemble: str = "true",
    ) -> str:
        result_index_value = self._parse_non_negative_int(result_index, -1)
        if result_index_value < 0:
            raise ValueError("result_index 必须是非负整数")
        item = await run_blocking(
            self.search_cache.get_search_result_item,
            search_id,
            result_index_value,
        )
        source_id = str(item.get("source_id") or "").strip()
        book_url = str(item.get("book_url") or "").strip()
        book_name = str(item.get("title") or "").strip()
        if not source_id:
            raise ValueError("缓存结果缺少 source_id，无法下载")
        if not book_url:
            raise ValueError(
                "缓存结果缺少 book_url，无法下载；请换一个 result_index 或换书源"
            )
        return await self.handle_novel_download_book(
            source_id,
            book_url,
            book_name,
            output_filename,
            auto_assemble,
        )

    async def handle_novel_resume_book_download(
        self,
        job_id: str,
        auto_assemble: str = "true",
    ) -> str:
        await self._ensure_rule_job_running(job_id, self._parse_bool(auto_assemble, True))
        return await self._render_job_status(job_id, created=False)

    async def handle_novel_start_download(
        self,
        book_name: str,
        toc_json: str,
        content_regex: str,
        title_regex: str = "",
        source_url: str = "",
        output_filename: str = "",
        encoding: str = "",
        auto_assemble: str = "true",
    ) -> str:
        toc = await run_blocking(json.loads, toc_json)
        job_info = await run_blocking(
            self.manager.create_job,
            book_name,
            toc,
            ExtractionRules(
                content_regex=content_regex,
                title_regex=title_regex,
            ),
            output_filename,
            source_url,
            encoding,
        )
        job_id = job_info["job_id"]
        await self._ensure_job_running(job_id, self._parse_bool(auto_assemble, True))
        return await self._render_job_status(job_id, created=job_info["created"])

    async def handle_novel_resume_download(
        self,
        job_id: str,
        auto_assemble: str = "true",
    ) -> str:
        await self._ensure_job_running(job_id, self._parse_bool(auto_assemble, True))
        return await self._render_job_status(job_id, created=False)

    async def handle_novel_download_status(
        self,
        job_id: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        if job_id:
            return await self._render_job_status(job_id, created=False)

        jobs = await run_blocking(self.manager.list_jobs)
        if not jobs:
            return "当前没有任何下载任务。"
        return await run_blocking(
            self.renderer.render_jobs_summary,
            jobs,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    async def handle_novel_assemble_book(self, job_id: str, cleanup_journal: str = "") -> str:
        status = await run_blocking(
            self.manager.assemble,
            job_id,
            self._parse_bool(
                cleanup_journal,
                self.manager.config.cleanup_journal_after_assemble,
            ),
        )
        return await run_blocking(self.renderer.render_status, status, False)

    async def handle_novel_list_jobs(self, limit: str = "", offset: str = "") -> str:
        jobs = await run_blocking(self.manager.list_jobs)
        return await run_blocking(
            self.renderer.render_jobs_summary,
            jobs,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    async def _ensure_job_running(self, job_id: str, auto_assemble: bool) -> None:
        existing = self._running_tasks.get(job_id)
        if existing and not existing.done():
            return

        task = asyncio.create_task(self._run_job(job_id, auto_assemble))
        self._running_tasks[job_id] = task

    async def _ensure_rule_job_running(self, job_id: str, auto_assemble: bool) -> None:
        existing = self._running_tasks.get(job_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_rule_job(job_id, auto_assemble))
        self._running_tasks[job_id] = task

    async def _run_job(self, job_id: str, auto_assemble: bool) -> None:
        try:
            await run_blocking(self.manager.download_missing, job_id)
            if auto_assemble:
                await run_blocking(
                    self.manager.assemble,
                    job_id,
                    self.manager.config.cleanup_journal_after_assemble,
                )
        except Exception as exc:
            await run_blocking(self._record_failed_state, job_id, str(exc))
            logger.exception("小说下载任务失败 job_id=%s error=%s", job_id, exc)

    async def _run_rule_job(self, job_id: str, auto_assemble: bool) -> None:
        try:
            await run_blocking(
                self.source_download_service.resume_book_job,
                job_id,
                auto_assemble,
            )
        except Exception as exc:
            await run_blocking(self._record_failed_state, job_id, str(exc))
            logger.exception("书源规则下载任务失败 job_id=%s error=%s", job_id, exc)

    async def _render_job_status(self, job_id: str, created: bool) -> str:
        status = await run_blocking(self.manager.get_status, job_id)
        return await run_blocking(self.renderer.render_status, status, created)

    def _record_failed_state(self, job_id: str, error: str) -> None:
        self.manager.record_state(job_id, "failed", error=error)

    def _record_preflight_success(self, preflight: dict[str, Any]) -> None:
        source_id = str(preflight.get("source_id") or "").strip()
        if not source_id:
            return
        self.source_health_store.record_success(
            source_id,
            "preflight",
            summary="目录预检成功",
            metadata={
                "sample_book_name": str(preflight.get("book_name") or "").strip(),
                "sample_book_url": str(preflight.get("book_url") or "").strip(),
                "toc_count": int(preflight.get("toc_count", 0) or 0),
            },
        )
        self.source_health_store.mark_unknown(
            source_id,
            "download",
            summary="已通过目录预检，等待正文下载结果",
        )

    def _record_preflight_failure(
        self,
        source_id: str,
        book_url: str,
        book_name: str,
        error: str,
    ) -> None:
        normalized_source_id = str(source_id or "").strip()
        if not normalized_source_id:
            return
        try:
            summary = self.source_registry.get_source_summary(normalized_source_id)
        except Exception:
            summary = {}
        metadata = {
            "sample_book_name": str(book_name or "").strip(),
            "sample_book_url": str(book_url or "").strip(),
        }
        if summary and not summary.get("supports_download", False):
            issues = "；".join(summary.get("issues") or []) or str(error or "").strip()
            self.source_health_store.mark_unsupported(
                normalized_source_id,
                "preflight",
                summary=issues,
                metadata=metadata,
            )
            self.source_health_store.mark_unsupported(
                normalized_source_id,
                "download",
                summary=issues,
                metadata=metadata,
            )
            return
        self.source_health_store.record_failure(
            normalized_source_id,
            "preflight",
            error_code="preflight_failed",
            error_summary=str(error or "").strip(),
            metadata=metadata,
        )
        self.source_health_store.mark_unknown(
            normalized_source_id,
            "download",
            summary="目录预检失败，未进入正文下载",
            metadata=metadata,
        )

    def _record_auto_download_attempts(self, orchestration: dict[str, Any]) -> None:
        skipped_candidates = list(orchestration.get("skipped_candidates") or [])
        for candidate in skipped_candidates:
            if candidate.get("supports_download"):
                continue
            source_id = str(candidate.get("source_id") or "").strip()
            if not source_id:
                continue
            summary = str(candidate.get("skip_reason") or "").strip()
            metadata = {
                "sample_book_name": str(candidate.get("title") or "").strip(),
                "sample_book_url": str(candidate.get("book_url") or "").strip(),
            }
            self.source_health_store.mark_unsupported(
                source_id,
                "preflight",
                summary=summary,
                metadata=metadata,
            )
            self.source_health_store.mark_unsupported(
                source_id,
                "download",
                summary=summary,
                metadata=metadata,
            )

        for attempt in list(orchestration.get("attempts") or []):
            outcome = str(attempt.get("outcome") or "").strip()
            preflight = dict(attempt.get("preflight") or {})
            if outcome in {"started", "job_create_failed"} and preflight:
                self._record_preflight_success(preflight)
                continue
            if outcome != "preflight_failed":
                continue
            self._record_preflight_failure(
                str(attempt.get("source_id") or "").strip(),
                str(attempt.get("book_url") or "").strip(),
                str(attempt.get("title") or "").strip(),
                str(attempt.get("error") or "").strip(),
            )

    def _parse_optional_int(self, value: str) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = int(text)
        return parsed if parsed > 0 else None

    def _parse_non_negative_int(self, value: str, default: int = 0) -> int:
        text = str(value or "").strip()
        if not text:
            return default
        parsed = int(text)
        return parsed if parsed >= 0 else default

    def _parse_bool(self, value: str, default: bool) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return default
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError("布尔参数仅支持 true/false/1/0/yes/no")

    def _parse_string_list(self, value: str) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]

    def _parse_config_refs(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item or "").strip()]
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item or "").strip()]
        parts = [part.strip() for part in text.splitlines() if part.strip()]
        if parts:
            return parts
        return [text]

    def _schedule_bootstrap_config_sources(self) -> None:
        source_refs = self._parse_config_refs(self.config.get("book_sources"))
        repo_refs = self._parse_config_refs(self.config.get("clean_rule_sources"))
        if not source_refs and not repo_refs:
            return

        self._bootstrap_done.clear()
        self._bootstrap_thread = threading.Thread(
            target=self._bootstrap_config_sources,
            args=(source_refs, repo_refs),
            name="{name}-bootstrap".format(name=PLUGIN_NAME),
            daemon=True,
        )
        self._bootstrap_thread.start()
        logger.info(
            "已在后台启动配置导入 pending_source_count=%s pending_clean_rule_count=%s",
            len(source_refs),
            len(repo_refs),
        )

    def _bootstrap_config_sources(
        self,
        source_refs: list[str],
        repo_refs: list[str],
    ) -> None:
        try:
            source_refs = self._filter_bootstrap_refs(source_refs, "book_sources")
            repo_refs = self._filter_bootstrap_refs(repo_refs, "clean_rule_sources")
            for source_ref in source_refs:
                self._run_bootstrap_source_import(source_ref)
            for repo_ref in repo_refs:
                self._run_bootstrap_clean_rule_import(repo_ref)
        finally:
            self._bootstrap_done.set()

    def _run_bootstrap_source_import(self, source_ref: str) -> None:
        signature = self._build_bootstrap_signature(source_ref)
        entry_id = self._build_bootstrap_entry_id(source_ref)
        started_at = time.time()
        self._save_bootstrap_result(
            "book_sources",
            entry_id,
            source_ref,
            signature,
            "running",
            started_at,
        )
        imported_count = 0
        try:
            source_text = load_text_argument(
                source_ref,
                self.manager.config.user_agent,
                self.manager.config.request_timeout,
                self.manager.config.default_encoding,
                self.manager.config.use_env_proxy,
            )
            result = self.source_registry.import_sources_from_text(source_text)
            probe_result = self._queue_probe_for_import_result(result)
            imported_count = int(result.get("imported_count", 0))
            logger.info(
                "从配置导入书源成功 source_ref=%s imported_count=%s queued_probe_count=%s",
                source_ref,
                imported_count,
                probe_result.get("queued_probe_count", 0),
            )
            self._save_bootstrap_result(
                "book_sources",
                entry_id,
                source_ref,
                signature,
                "success",
                started_at,
                imported_count=imported_count,
                queued_probe_count=int(probe_result.get("queued_probe_count", 0) or 0),
            )
        except Exception as exc:
            logger.warning("从配置导入书源失败 source_ref=%s error=%s", source_ref, exc)
            self._save_bootstrap_result(
                "book_sources",
                entry_id,
                source_ref,
                signature,
                "failed",
                started_at,
                error=str(exc),
                imported_count=imported_count,
            )

    def _run_bootstrap_clean_rule_import(self, repo_ref: str) -> None:
        signature = self._build_bootstrap_signature(repo_ref)
        entry_id = self._build_bootstrap_entry_id(repo_ref)
        started_at = time.time()
        self._save_bootstrap_result(
            "clean_rule_sources",
            entry_id,
            repo_ref,
            signature,
            "running",
            started_at,
        )
        rule_count = 0
        try:
            repo_text = load_text_argument(
                repo_ref,
                self.manager.config.user_agent,
                self.manager.config.request_timeout,
                self.manager.config.default_encoding,
                self.manager.config.use_env_proxy,
            )
            record = self.clean_rule_store.import_rules_from_text(
                repo_text,
                "",
                repo_ref,
            )
            rule_count = int(record.get("rule_count", 0))
            logger.info(
                "从配置导入净化规则成功 repo_ref=%s rule_count=%s",
                repo_ref,
                rule_count,
            )
            self._save_bootstrap_result(
                "clean_rule_sources",
                entry_id,
                repo_ref,
                signature,
                "success",
                started_at,
                rule_count=rule_count,
            )
        except Exception as exc:
            logger.warning("从配置导入净化规则失败 repo_ref=%s error=%s", repo_ref, exc)
            self._save_bootstrap_result(
                "clean_rule_sources",
                entry_id,
                repo_ref,
                signature,
                "failed",
                started_at,
                error=str(exc),
                rule_count=rule_count,
            )

    def wait_for_bootstrap(self, timeout: float | None = None) -> bool:
        thread = self._bootstrap_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def wait_for_probe(self, timeout: float | None = None) -> bool:
        wait_for_idle = getattr(self.source_probe_service, "wait_for_idle", None)
        if not callable(wait_for_idle):
            return True
        return bool(wait_for_idle(timeout))

    def _filter_bootstrap_refs(self, refs: list[str], section: str) -> list[str]:
        if section == "book_sources" and not self.source_registry.list_sources():
            return refs
        if section == "clean_rule_sources" and not self.clean_rule_store.list_repositories():
            return refs

        state = self._load_bootstrap_state()
        pending: list[str] = []
        skipped_count = 0
        for ref in refs:
            entry = state.get(section, {}).get(self._build_bootstrap_entry_id(ref), {})
            signature = self._build_bootstrap_signature(ref)
            if entry.get("signature") == signature:
                status = str(entry.get("status") or "")
                if status == "success":
                    skipped_count += 1
                    logger.info(
                        "跳过重复的配置导入 section=%s ref=%s",
                        section,
                        self._short_bootstrap_ref(ref),
                    )
                    continue
                if status == "running" and (time.time() - float(entry.get("updated_at", 0.0))) < 1800:
                    skipped_count += 1
                    logger.info(
                        "检测到已有后台导入正在进行，跳过重复启动 section=%s ref=%s",
                        section,
                        self._short_bootstrap_ref(ref),
                    )
                    continue
            pending.append(ref)
        if skipped_count:
            logger.info(
                "配置导入去重完成 section=%s skipped_count=%s pending_count=%s",
                section,
                skipped_count,
                len(pending),
            )
        return pending

    def _build_bootstrap_entry_id(self, ref: str) -> str:
        return hashlib.sha1(str(ref).encode("utf-8")).hexdigest()

    def _build_bootstrap_signature(self, ref: str) -> str:
        text = str(ref or "").strip()
        if not text:
            return ""

        try:
            path = Path(text).expanduser()
        except (OSError, ValueError):
            path = None

        if path is not None:
            try:
                if path.is_file():
                    stat = path.stat()
                    return "file:{path}:{mtime}:{size}".format(
                        path=path.resolve(),
                        mtime=stat.st_mtime_ns,
                        size=stat.st_size,
                    )
            except OSError:
                pass

        if text.startswith(("http://", "https://", "file://")):
            return "url:{text}".format(text=text)
        return "inline:{digest}".format(
            digest=hashlib.sha1(text.encode("utf-8")).hexdigest(),
        )

    def _short_bootstrap_ref(self, ref: str) -> str:
        text = str(ref or "").strip()
        if len(text) <= 160:
            return text
        return text[:157] + "..."

    def _queue_probe_for_import_result(self, result: dict[str, Any]) -> dict[str, int]:
        if not self.auto_probe_on_import:
            return {
                "queued_probe_count": 0,
                "probe_queue_size": 0,
            }
        sources = list(result.get("sources") or [])
        source_ids = [
            str(item.get("source_id") or "").strip()
            for item in sources
            if str(item.get("source_id") or "").strip()
        ]
        if not source_ids:
            return {
                "queued_probe_count": 0,
                "probe_queue_size": 0,
            }
        queued = self.source_probe_service.enqueue_sources(source_ids)
        return {
            "queued_probe_count": int(queued.get("queued_count", 0) or 0),
            "probe_queue_size": int(queued.get("queue_size", 0) or 0),
        }

    def _load_bootstrap_state(self) -> dict[str, Any]:
        if not self._bootstrap_state_path.exists():
            return self._make_bootstrap_state()
        try:
            with open(self._bootstrap_state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except Exception:
            logger.warning("启动导入状态文件损坏，已回退到空状态")
            return self._make_bootstrap_state()
        if not isinstance(state, dict):
            return self._make_bootstrap_state()
        state.setdefault("schema_version", 1)
        state.setdefault("updated_at", 0.0)
        state.setdefault("book_sources", {})
        state.setdefault("clean_rule_sources", {})
        return state

    def _make_bootstrap_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "updated_at": 0.0,
            "book_sources": {},
            "clean_rule_sources": {},
        }

    def _save_bootstrap_result(
        self,
        section: str,
        entry_id: str,
        ref: str,
        signature: str,
        status: str,
        started_at: float,
        **extra: Any,
    ) -> None:
        state = self._load_bootstrap_state()
        state.setdefault(section, {})[entry_id] = {
            "ref_preview": self._short_bootstrap_ref(ref),
            "signature": signature,
            "status": status,
            "started_at": started_at,
            "updated_at": time.time(),
            **extra,
        }
        state["updated_at"] = time.time()
        self._write_json_file(self._bootstrap_state_path, state)

    def _write_json_file(self, path: Path, payload: Any) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)

    async def _load_text_argument(self, value: str) -> str:
        return await run_blocking(
            load_text_argument,
            value,
            self.manager.config.user_agent,
            self.manager.config.request_timeout,
            self.manager.config.default_encoding,
            self.manager.config.use_env_proxy,
        )
