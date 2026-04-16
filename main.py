from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

try:
    from astrbot.api import llm_tool as astrbot_llm_tool
except ImportError:
    astrbot_llm_tool = None

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger(__name__)

from .core.download_manager import ExtractionRules, NovelDownloadManager, RuntimeConfig
from .core.rule_engine import RuleEngine, RuleEngineConfig
from .core.search_service import SearchService, SearchServiceConfig
from .core.source_registry import SourceRegistry


def compat_llm_tool(name: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if astrbot_llm_tool is not None:
            return astrbot_llm_tool(name=name)(func)

        llm_tool_factory = getattr(filter, "llm_tool", None)
        if llm_tool_factory is None:
            return func

        for args, kwargs in (
            ((), {"name": name}),
            ((name,), {}),
            ((), {}),
        ):
            try:
                return llm_tool_factory(*args, **kwargs)(func)
            except TypeError:
                continue
        return func

    return decorator


@register(
    "astrbot_plugin_webnovel_downloader",
    "OpenAI",
    "网文下载器：基于单文件 journal 的纯 Python 网文下载与装订插件，支持断点续传、绝对有序输出与函数工具调用",
    "0.1.0",
    "https://github.com/dianhuaeven/astrbot_plugin_webnovel_downloader",
)
class JsonlNovelDownloaderPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        plugin_data_dir = StarTools.get_data_dir()
        self.manager = NovelDownloadManager(
            plugin_data_dir,
            RuntimeConfig(
                max_workers=int(self.config.get("max_workers", 6)),
                request_timeout=float(self.config.get("request_timeout", 20.0)),
                max_retries=int(self.config.get("max_retries", 3)),
                retry_backoff=float(self.config.get("retry_backoff", 1.6)),
                journal_fsync=bool(self.config.get("journal_fsync", False)),
                default_encoding=str(self.config.get("default_encoding", "")).strip(),
                preview_chars=int(self.config.get("preview_chars", 4000)),
                auto_assemble=bool(self.config.get("auto_assemble", True)),
                cleanup_journal_after_assemble=bool(
                    self.config.get("cleanup_journal_after_assemble", False)
                ),
                user_agent=str(self.config.get("user_agent", "")).strip()
                or RuntimeConfig().user_agent,
            ),
        )
        self.source_registry = SourceRegistry(plugin_data_dir)
        self.search_service = SearchService(
            self.source_registry,
            RuleEngine(
                RuleEngineConfig(
                    request_timeout=float(self.config.get("request_timeout", 20.0)),
                    user_agent=str(self.config.get("user_agent", "")).strip()
                    or RuntimeConfig().user_agent,
                )
            ),
            SearchServiceConfig(
                max_workers=max(1, min(8, int(self.config.get("max_workers", 6)))),
            ),
        )
        self._running_tasks: dict[str, asyncio.Task] = {}
        logger.info("网文下载器初始化完成")

    @compat_llm_tool(name="novel_fetch_preview")
    async def novel_fetch_preview(
        self, url: str, encoding: str = "", max_chars: str = ""
    ) -> str:
        """
        抓取网页预览，帮助分析目录页或章节页结构。

        Args:
            url(string): 目标网页地址。
            encoding(string): 可选，强制指定编码，例如 utf-8 或 gb18030。
            max_chars(string): 可选，最多返回多少字符；留空或填 0 表示使用插件默认值。
        """
        limit = self._parse_optional_int(max_chars)
        preview = await asyncio.to_thread(
            self.manager.fetch_preview,
            url,
            encoding,
            limit,
        )
        return json.dumps(preview, ensure_ascii=False, indent=2)

    @compat_llm_tool(name="novel_import_sources")
    async def novel_import_sources(self, source_json: str) -> str:
        """
        导入 Legado/阅读风格书源 JSON。

        Args:
            source_json(string): 单个书源对象、书源数组，或带 sources 字段的 JSON 字符串。
        """
        result = await asyncio.to_thread(
            self.source_registry.import_sources_from_text,
            source_json,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @compat_llm_tool(name="novel_list_sources")
    async def novel_list_sources(self, enabled_only: str = "") -> str:
        """
        列出已导入书源。

        Args:
            enabled_only(string): 是否只显示启用书源，支持 true/false/1/0/yes/no。
        """
        result = await asyncio.to_thread(
            self.source_registry.list_sources,
            self._parse_bool(enabled_only, False),
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @compat_llm_tool(name="novel_enable_source")
    async def novel_enable_source(self, source_id: str, enabled: str = "true") -> str:
        """
        启用或禁用一个书源。

        Args:
            source_id(string): 书源 ID。
            enabled(string): 是否启用，支持 true/false/1/0/yes/no。
        """
        result = await asyncio.to_thread(
            self.source_registry.set_enabled,
            source_id,
            self._parse_bool(enabled, True),
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @compat_llm_tool(name="novel_remove_source")
    async def novel_remove_source(self, source_id: str) -> str:
        """
        删除一个已导入的书源。

        Args:
            source_id(string): 书源 ID。
        """
        result = await asyncio.to_thread(self.source_registry.remove_source, source_id)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @compat_llm_tool(name="novel_search_books")
    async def novel_search_books(
        self,
        keyword: str,
        source_ids_json: str = "",
        limit: str = "",
        include_disabled: str = "",
    ) -> str:
        """
        按书名跨书源搜索小说。

        Args:
            keyword(string): 搜索关键词，通常是书名。
            source_ids_json(string): 可选，JSON 数组或逗号分隔的书源 ID 列表。
            limit(string): 可选，最多返回多少条结果。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
        """
        source_ids = self._parse_string_list(source_ids_json)
        result = await asyncio.to_thread(
            self.search_service.search,
            keyword,
            source_ids or None,
            self._parse_optional_int(limit) or 20,
            self._parse_bool(include_disabled, False),
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @compat_llm_tool(name="novel_start_download")
    async def novel_start_download(
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
        """
        创建并启动一个小说下载任务。

        Args:
            book_name(string): 书名。
            toc_json(string): 章节目录 JSON 字符串，格式为 [{"title":"第1章","url":"https://..."}, ...]。
            content_regex(string): 用于提取正文的正则，优先使用第一个捕获组。
            title_regex(string): 可选，用于提取章节标题的正则。
            source_url(string): 可选，目录页地址，仅用于记录来源。
            output_filename(string): 可选，自定义输出 TXT 文件名，不带路径。
            encoding(string): 可选，强制指定网页编码。
            auto_assemble(string): 是否自动组装，支持 true/false/1/0/yes/no。
        """
        toc = json.loads(toc_json)
        job_info = await asyncio.to_thread(
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
        return self._render_status(status, created=job_info["created"])

    @compat_llm_tool(name="novel_resume_download")
    async def novel_resume_download(self, job_id: str, auto_assemble: str = "true") -> str:
        """
        恢复一个已存在的下载任务，只抓取缺失章节。

        Args:
            job_id(string): 任务 ID。
            auto_assemble(string): 是否自动组装，支持 true/false/1/0/yes/no。
        """
        await self._ensure_job_running(job_id, self._parse_bool(auto_assemble, True))
        status = self.manager.get_status(job_id)
        return self._render_status(status, created=False)

    @compat_llm_tool(name="novel_download_status")
    async def novel_download_status(self, job_id: str = "") -> str:
        """
        查询任务状态；如果未传 job_id，则返回所有任务概览。

        Args:
            job_id(string): 可选，任务 ID。
        """
        if job_id:
            status = self.manager.get_status(job_id)
            return self._render_status(status, created=False)

        jobs = await asyncio.to_thread(self.manager.list_jobs)
        if not jobs:
            return "当前没有任何下载任务。"
        return json.dumps(jobs, ensure_ascii=False, indent=2)

    @compat_llm_tool(name="novel_assemble_book")
    async def novel_assemble_book(
        self, job_id: str, cleanup_journal: str = ""
    ) -> str:
        """
        将一个已下载完成的任务组装成最终 TXT。

        Args:
            job_id(string): 任务 ID。
            cleanup_journal(string): 是否删除 JSONL journal，支持 true/false/1/0/yes/no。
        """
        status = await asyncio.to_thread(
            self.manager.assemble,
            job_id,
            self._parse_bool(
                cleanup_journal,
                self.manager.config.cleanup_journal_after_assemble,
            ),
        )
        return self._render_status(status, created=False)

    @compat_llm_tool(name="novel_list_jobs")
    async def novel_list_jobs(self) -> str:
        """
        列出当前插件数据目录下的所有小说下载任务。
        """
        jobs = await asyncio.to_thread(self.manager.list_jobs)
        return json.dumps(jobs, ensure_ascii=False, indent=2)

    @filter.command("novel_jobs")
    async def novel_jobs_command(self, event):
        jobs = await asyncio.to_thread(self.manager.list_jobs)
        if not jobs:
            yield event.plain_result("当前没有任何下载任务。")
            return
        lines = []
        for item in jobs:
            lines.append(
                f"{item.get('job_id')} | {item.get('state')} | "
                f"{item.get('completed_chapters', 0)}/{item.get('total_chapters', 0)}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("novel_sources")
    async def novel_sources_command(self, event):
        sources = await asyncio.to_thread(self.source_registry.list_sources, False)
        if not sources:
            yield event.plain_result("当前没有已导入书源。")
            return
        lines = []
        for item in sources:
            state = "on" if item.get("enabled") else "off"
            lines.append(f"{item.get('source_id')} | {state} | {item.get('name')}")
        yield event.plain_result("\n".join(lines))

    async def _ensure_job_running(self, job_id: str, auto_assemble: bool) -> None:
        existing = self._running_tasks.get(job_id)
        if existing and not existing.done():
            return

        task = asyncio.create_task(self._run_job(job_id, auto_assemble))
        self._running_tasks[job_id] = task

    async def _run_job(self, job_id: str, auto_assemble: bool) -> None:
        try:
            await asyncio.to_thread(self.manager.download_missing, job_id)
            should_assemble = auto_assemble
            if should_assemble:
                await asyncio.to_thread(
                    self.manager.assemble,
                    job_id,
                    self.manager.config.cleanup_journal_after_assemble,
                )
        except Exception as exc:
            self.manager.record_state(job_id, "failed", error=str(exc))
            logger.exception("小说下载任务失败 job_id=%s error=%s", job_id, exc)

    def _parse_optional_int(self, value: str) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = int(text)
        return parsed if parsed > 0 else None

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

    def _render_status(self, status: dict[str, Any], created: bool) -> str:
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
