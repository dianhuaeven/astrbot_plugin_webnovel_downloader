from __future__ import annotations

import asyncio
import json
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
        self.search_service = runtime.search_service
        self.source_download_service = runtime.source_download_service
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
        self._bootstrap_config_sources()
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
        return self.renderer.to_json_text(preview)

    async def handle_novel_import_sources(self, source_json: str) -> str:
        source_text = await self._load_text_argument(source_json)
        result = await run_blocking(
            self.source_registry.import_sources_from_text,
            source_text,
        )
        return self.renderer.render_import_summary(result)

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
        return self.renderer.render_clean_rule_import_summary(record)

    async def handle_novel_list_clean_rules(self, limit: str = "", offset: str = "") -> str:
        repos = await run_blocking(self.clean_rule_store.list_repositories)
        return self.renderer.render_clean_rule_list_summary(
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
        return self.renderer.render_sources_summary(
            result,
            enabled_only_value,
            limit_value,
            offset_value,
        )

    async def handle_novel_enable_source(self, source_id: str, enabled: str = "true") -> str:
        result = await run_blocking(
            self.source_registry.set_enabled,
            source_id,
            self._parse_bool(enabled, True),
        )
        return self.renderer.render_source_change_summary("set_enabled", result)

    async def handle_novel_remove_source(self, source_id: str) -> str:
        result = await run_blocking(self.source_registry.remove_source, source_id)
        return self.renderer.render_source_change_summary("removed", result)

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
        return self.renderer.render_search_summary_with_cache(result, cache_record)

    async def handle_novel_list_searches(self, limit: str = "", offset: str = "") -> str:
        searches = await run_blocking(self.search_cache.list_searches)
        return self.renderer.render_search_cache_list_summary(
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
        return self.renderer.render_cached_search_results(
            payload,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    async def handle_novel_download_book(
        self,
        source_id: str,
        book_url: str,
        book_name: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
    ) -> str:
        job_info = await run_blocking(
            self.source_download_service.create_book_job,
            source_id,
            book_url,
            book_name,
            output_filename,
        )
        job_id = job_info["job_id"]
        await self._ensure_rule_job_running(job_id, self._parse_bool(auto_assemble, True))
        status = self.manager.get_status(job_id)
        return self.renderer.render_status(status, created=job_info["created"])

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
        status = self.manager.get_status(job_id)
        return self.renderer.render_status(status, created=False)

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
        toc = json.loads(toc_json)
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
        status = self.manager.get_status(job_id)
        return self.renderer.render_status(status, created=job_info["created"])

    async def handle_novel_resume_download(
        self,
        job_id: str,
        auto_assemble: str = "true",
    ) -> str:
        await self._ensure_job_running(job_id, self._parse_bool(auto_assemble, True))
        status = self.manager.get_status(job_id)
        return self.renderer.render_status(status, created=False)

    async def handle_novel_download_status(
        self,
        job_id: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        if job_id:
            status = self.manager.get_status(job_id)
            return self.renderer.render_status(status, created=False)

        jobs = await run_blocking(self.manager.list_jobs)
        if not jobs:
            return "当前没有任何下载任务。"
        return self.renderer.render_jobs_summary(
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
        return self.renderer.render_status(status, created=False)

    async def handle_novel_list_jobs(self, limit: str = "", offset: str = "") -> str:
        jobs = await run_blocking(self.manager.list_jobs)
        return self.renderer.render_jobs_summary(
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
            self.manager.record_state(job_id, "failed", error=str(exc))
            logger.exception("小说下载任务失败 job_id=%s error=%s", job_id, exc)

    async def _run_rule_job(self, job_id: str, auto_assemble: bool) -> None:
        try:
            await run_blocking(
                self.source_download_service.resume_book_job,
                job_id,
                auto_assemble,
            )
        except Exception as exc:
            self.manager.record_state(job_id, "failed", error=str(exc))
            logger.exception("书源规则下载任务失败 job_id=%s error=%s", job_id, exc)

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

    def _bootstrap_config_sources(self) -> None:
        for source_ref in self._parse_config_refs(self.config.get("book_sources")):
            try:
                source_text = load_text_argument(
                    source_ref,
                    self.manager.config.user_agent,
                    self.manager.config.request_timeout,
                    self.manager.config.default_encoding,
                )
                result = self.source_registry.import_sources_from_text(source_text)
                logger.info(
                    "从配置导入书源成功 source_ref=%s imported_count=%s",
                    source_ref,
                    result.get("imported_count", 0),
                )
            except Exception as exc:
                logger.warning("从配置导入书源失败 source_ref=%s error=%s", source_ref, exc)

        for repo_ref in self._parse_config_refs(self.config.get("clean_rule_sources")):
            try:
                repo_text = load_text_argument(
                    repo_ref,
                    self.manager.config.user_agent,
                    self.manager.config.request_timeout,
                    self.manager.config.default_encoding,
                )
                record = self.clean_rule_store.import_rules_from_text(
                    repo_text,
                    "",
                    repo_ref,
                )
                logger.info(
                    "从配置导入净化规则成功 repo_ref=%s rule_count=%s",
                    repo_ref,
                    record.get("rule_count", 0),
                )
            except Exception as exc:
                logger.warning("从配置导入净化规则失败 repo_ref=%s error=%s", repo_ref, exc)

    async def _load_text_argument(self, value: str) -> str:
        return await run_blocking(
            load_text_argument,
            value,
            self.manager.config.user_agent,
            self.manager.config.request_timeout,
            self.manager.config.default_encoding,
        )
