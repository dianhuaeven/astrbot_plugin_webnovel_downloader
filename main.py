from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrbot.api.event import AstrMessageEvent, filter
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
from .core.source_downloader import SourceDownloadConfig, SourceDownloadService
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


async def run_blocking(func: Callable, *args):
    to_thread = getattr(asyncio, "to_thread", None)
    if to_thread is not None:
        return await to_thread(func, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


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
        plugin_data_dir = Path(StarTools.get_data_dir())
        self.plugin_data_dir = plugin_data_dir
        self.reports_dir = self.plugin_data_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.max_tool_response_chars = max(800, int(self.config.get("max_tool_response_chars", 2800)))
        self.max_tool_preview_items = max(1, int(self.config.get("max_tool_preview_items", 8)))
        self.max_tool_preview_text = max(60, int(self.config.get("max_tool_preview_text", 180)))
        self.max_preview_fetch_chars = max(300, int(self.config.get("max_preview_fetch_chars", 1800)))
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
        self.source_download_service = SourceDownloadService(
            self.source_registry,
            self.search_service.engine,
            self.manager,
            SourceDownloadConfig(
                max_workers=max(1, min(8, int(self.config.get("max_workers", 6)))),
            ),
        )
        self._running_tasks: dict[str, asyncio.Task] = {}
        logger.info("网文下载器初始化完成")

    @compat_llm_tool(name="novel_fetch_preview")
    async def novel_fetch_preview(
        self, event: AstrMessageEvent, url: str, encoding: str = "", max_chars: str = ""
    ) -> str:
        """
        抓取网页预览，帮助分析目录页或章节页结构。

        Args:
            url(string): 目标网页地址。
            encoding(string): 可选，强制指定编码，例如 utf-8 或 gb18030。
            max_chars(string): 可选，最多返回多少字符；留空或填 0 表示使用插件默认值。
        """
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
        preview["html_preview"] = self._truncate_text(preview.get("html_preview", ""), limit)
        preview["text_preview"] = self._truncate_text(preview.get("text_preview", ""), limit)
        preview["applied_max_chars"] = limit
        return self._to_json_text(preview)

    @compat_llm_tool(name="novel_import_sources")
    async def novel_import_sources(self, event: AstrMessageEvent, source_json: str) -> str:
        """
        导入 Legado/阅读风格书源 JSON。

        Args:
            source_json(string): 单个书源对象、书源数组，或带 sources 字段的 JSON 字符串。
        """
        source_json = await self._load_text_argument(source_json)
        result = await run_blocking(
            self.source_registry.import_sources_from_text,
            source_json,
        )
        return self._render_import_summary(result)

    @compat_llm_tool(name="novel_list_sources")
    async def novel_list_sources(
        self,
        event: AstrMessageEvent,
        enabled_only: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        列出已导入书源。

        Args:
            enabled_only(string): 是否只显示启用书源，支持 true/false/1/0/yes/no。
            limit(string): 可选，本次最多返回多少条预览。
            offset(string): 可选，从第几条开始返回，支持 0、10、20 这类非负整数。
        """
        enabled_only_value = self._parse_bool(enabled_only, False)
        limit_value = self._parse_optional_int(limit) or self.max_tool_preview_items
        offset_value = self._parse_non_negative_int(offset, 0)
        result = await run_blocking(
            self.source_registry.list_sources,
            enabled_only_value,
        )
        return self._render_sources_summary(
            result, enabled_only_value, limit_value, offset_value
        )

    @compat_llm_tool(name="novel_enable_source")
    async def novel_enable_source(
        self, event: AstrMessageEvent, source_id: str, enabled: str = "true"
    ) -> str:
        """
        启用或禁用一个书源。

        Args:
            source_id(string): 书源 ID。
            enabled(string): 是否启用，支持 true/false/1/0/yes/no。
        """
        result = await run_blocking(
            self.source_registry.set_enabled,
            source_id,
            self._parse_bool(enabled, True),
        )
        return self._render_source_change_summary("set_enabled", result)

    @compat_llm_tool(name="novel_remove_source")
    async def novel_remove_source(self, event: AstrMessageEvent, source_id: str) -> str:
        """
        删除一个已导入的书源。

        Args:
            source_id(string): 书源 ID。
        """
        result = await run_blocking(self.source_registry.remove_source, source_id)
        return self._render_source_change_summary("removed", result)

    @compat_llm_tool(name="novel_search_books")
    async def novel_search_books(
        self,
        event: AstrMessageEvent,
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
        result = await run_blocking(
            self.search_service.search,
            keyword,
            source_ids or None,
            self._parse_optional_int(limit) or 20,
            self._parse_bool(include_disabled, False),
        )
        return self._render_search_summary(result)

    @compat_llm_tool(name="novel_download_book")
    async def novel_download_book(
        self,
        event: AstrMessageEvent,
        source_id: str,
        book_url: str,
        book_name: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
    ) -> str:
        """
        基于已导入书源的规则，从书籍详情页自动抓目录并下载 TXT。

        Args:
            source_id(string): 书源 ID。
            book_url(string): 书籍详情页地址，通常来自 novel_search_books 的 book_url。
            book_name(string): 可选，手动指定书名。
            output_filename(string): 可选，自定义输出 TXT 文件名。
            auto_assemble(string): 是否自动组装 TXT，支持 true/false/1/0/yes/no。
        """
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
        return self._render_status(status, created=job_info["created"])

    @compat_llm_tool(name="novel_resume_book_download")
    async def novel_resume_book_download(
        self,
        event: AstrMessageEvent,
        job_id: str,
        auto_assemble: str = "true",
    ) -> str:
        """
        恢复一个书源规则下载任务，只补缺失章节。

        Args:
            job_id(string): 任务 ID。
            auto_assemble(string): 是否自动组装 TXT，支持 true/false/1/0/yes/no。
        """
        await self._ensure_rule_job_running(job_id, self._parse_bool(auto_assemble, True))
        status = self.manager.get_status(job_id)
        return self._render_status(status, created=False)

    @compat_llm_tool(name="novel_start_download")
    async def novel_start_download(
        self,
        event: AstrMessageEvent,
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
        return self._render_status(status, created=job_info["created"])

    @compat_llm_tool(name="novel_resume_download")
    async def novel_resume_download(
        self, event: AstrMessageEvent, job_id: str, auto_assemble: str = "true"
    ) -> str:
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
    async def novel_download_status(
        self,
        event: AstrMessageEvent,
        job_id: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        查询任务状态；如果未传 job_id，则返回所有任务概览。

        Args:
            job_id(string): 可选，任务 ID。
            limit(string): 可选，当未传 job_id 时，本次最多返回多少条任务预览。
            offset(string): 可选，当未传 job_id 时，从第几条任务开始返回。
        """
        if job_id:
            status = self.manager.get_status(job_id)
            return self._render_status(status, created=False)

        jobs = await run_blocking(self.manager.list_jobs)
        if not jobs:
            return "当前没有任何下载任务。"
        return self._render_jobs_summary(
            jobs,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    @compat_llm_tool(name="novel_assemble_book")
    async def novel_assemble_book(
        self, event: AstrMessageEvent, job_id: str, cleanup_journal: str = ""
    ) -> str:
        """
        将一个已下载完成的任务组装成最终 TXT。

        Args:
            job_id(string): 任务 ID。
            cleanup_journal(string): 是否删除 JSONL journal，支持 true/false/1/0/yes/no。
        """
        status = await run_blocking(
            self.manager.assemble,
            job_id,
            self._parse_bool(
                cleanup_journal,
                self.manager.config.cleanup_journal_after_assemble,
            ),
        )
        return self._render_status(status, created=False)

    @compat_llm_tool(name="novel_list_jobs")
    async def novel_list_jobs(
        self, event: AstrMessageEvent, limit: str = "", offset: str = ""
    ) -> str:
        """
        列出当前插件数据目录下的所有小说下载任务。
        """
        jobs = await run_blocking(self.manager.list_jobs)
        return self._render_jobs_summary(
            jobs,
            self._parse_optional_int(limit) or self.max_tool_preview_items,
            self._parse_non_negative_int(offset, 0),
        )

    @filter.command("novel_jobs")
    async def novel_jobs_command(self, event):
        jobs = await run_blocking(self.manager.list_jobs)
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
        sources = await run_blocking(self.source_registry.list_sources, False)
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

    async def _ensure_rule_job_running(self, job_id: str, auto_assemble: bool) -> None:
        existing = self._running_tasks.get(job_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_rule_job(job_id, auto_assemble))
        self._running_tasks[job_id] = task

    async def _run_job(self, job_id: str, auto_assemble: bool) -> None:
        try:
            await run_blocking(self.manager.download_missing, job_id)
            should_assemble = auto_assemble
            if should_assemble:
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

    async def _load_text_argument(self, value: str) -> str:
        text = str(value or "").strip()
        if text.startswith(("http://", "https://", "file://")):
            return await run_blocking(self._fetch_raw_text, text)
        return text

    def _fetch_raw_text(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": self.manager.config.user_agent,
            },
        )
        try:
            with urlopen(request, timeout=self.manager.config.request_timeout) as response:
                body = response.read()
                encoding = (
                    response.headers.get_content_charset()
                    or self.manager.config.default_encoding
                    or "utf-8"
                )
        except HTTPError as exc:
            raise ValueError(self._format_remote_fetch_error(url, exc.code, str(exc.reason))) from exc
        except URLError as exc:
            raise ValueError("网络错误: {reason}".format(reason=exc.reason)) from exc
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            return body.decode("utf-8", errors="replace")

    def _format_remote_fetch_error(self, url: str, code: int, reason: str) -> str:
        message = "HTTP {code}: {reason}".format(code=code, reason=reason)
        if code == 400 and "jsdelivr" in url:
            return (
                "{base}。提示：jsDelivr 的 GitHub 文件地址通常应为 "
                "https://cdn.jsdelivr.net/gh/<user>/<repo>@<branch>/<path/to/file> ，"
                "或 https://gcore.jsdelivr.net/gh/<user>/<repo>@<branch>/<path/to/file> "
                "；你当前这条链接看起来缺少 repo 名或分支信息"
            ).format(base=message)
        if code != 404:
            return message

        tips = []
        if "raw.githubusercontent.com" in url:
            tips.append("GitHub raw 地址通常需要包含分支名，例如 /main/ 或 /master/")
            tips.append("也请确认文件路径是否真的在该目录下")
        if "github.com" in url and "/blob/" in url:
            tips.append("你传的是 GitHub 页面链接，建议改成 raw 链接或仓库中的实际文件直链")
        if tips:
            return "{base}。提示：{tips}".format(base=message, tips="；".join(tips))
        return message

    def _to_json_text(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _truncate_text(self, value: Any, limit: int | None = None) -> str:
        text = str(value or "")
        max_length = limit or self.max_tool_preview_text
        if len(text) <= max_length:
            return text
        return text[: max(1, max_length - 1)] + "…"

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
        return {
            "source_id": item.get("source_id", ""),
            "name": item.get("name", ""),
            "enabled": bool(item.get("enabled", False)),
            "search_uses_js": bool(item.get("search_uses_js", False)),
            "download_uses_js": bool(item.get("download_uses_js", False)),
            "has_js_lib": bool(item.get("has_js_lib", False)),
            "has_login_flow": bool(item.get("has_login_flow", False)),
            "supports_search": bool(item.get("supports_search", False)),
            "supports_download": bool(item.get("supports_download", False)),
            "group": self._truncate_text(item.get("group", ""), 60),
            "search_url": self._truncate_text(item.get("search_url", ""), 120),
            "issues": [self._truncate_text(issue, 80) for issue in list(item.get("issues") or [])[:3]],
        }

    def _compact_search_result(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": item.get("source_id", ""),
            "source_name": item.get("source_name", ""),
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "book_url": item.get("book_url", ""),
            "kind": self._truncate_text(item.get("kind", ""), 40),
            "last_chapter": self._truncate_text(item.get("last_chapter", ""), 60),
            "word_count": item.get("word_count", ""),
            "intro": self._truncate_text(item.get("intro", ""), self.max_tool_preview_text),
        }

    def _compact_search_notice(self, item: dict[str, Any], message_key: str) -> dict[str, Any]:
        return {
            "source_id": item.get("source_id", ""),
            "source_name": item.get("source_name", ""),
            message_key: self._truncate_text(item.get(message_key, ""), self.max_tool_preview_text),
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

    def _render_source_change_summary(self, action: str, source: dict[str, Any]) -> str:
        summary = {
            "action": action,
            "registry_path": str(self.source_registry.registry_path),
            "source": self._compact_source(source),
        }
        return self._to_json_text(summary)

    def _render_import_summary(self, result: dict[str, Any]) -> str:
        sources = list(result.get("sources") or [])
        warnings = list(result.get("warnings") or [])
        source_preview_count = min(self.max_tool_preview_items, len(sources))
        warning_preview_count = min(self.max_tool_preview_items, len(warnings))
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
                    self._truncate_text(item, self.max_tool_preview_text)
                    for item in warnings[:warning_preview_count]
                ],
                "remaining_source_count": max(0, len(sources) - source_preview_count),
                "remaining_warning_count": max(0, len(warnings) - warning_preview_count),
            }
            if report_path:
                summary["report_path"] = report_path
            text = self._to_json_text(summary)
            if len(text) <= self.max_tool_response_chars:
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

    def _render_sources_summary(
        self,
        sources: list[dict[str, Any]],
        enabled_only: bool,
        limit: int,
        offset: int,
    ) -> str:
        total = len(sources)
        sliced = sources[offset : offset + limit]
        preview_count = min(self.max_tool_preview_items, len(sliced))
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
            text = self._to_json_text(summary)
            if len(text) <= self.max_tool_response_chars:
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

    def _render_search_summary(self, result: dict[str, Any]) -> str:
        results = list(result.get("results") or [])
        skipped_sources = list(result.get("skipped_sources") or [])
        errors = list(result.get("errors") or [])
        result_preview_count = min(self.max_tool_preview_items, len(results))
        skipped_preview_count = min(self.max_tool_preview_items, len(skipped_sources))
        error_preview_count = min(self.max_tool_preview_items, len(errors))
        report_path = ""

        while True:
            compact = {
                "keyword": result.get("keyword", ""),
                "searched_sources": result.get("searched_sources", 0),
                "successful_sources": result.get("successful_sources", 0),
                "result_count": len(results),
                "skipped_source_count": len(skipped_sources),
                "error_count": len(errors),
                "results": [self._compact_search_result(item) for item in results[:result_preview_count]],
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
            text = self._to_json_text(compact)
            if len(text) <= self.max_tool_response_chars:
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

    def _render_jobs_summary(self, jobs: list[dict[str, Any]], limit: int, offset: int) -> str:
        total = len(jobs)
        sliced = jobs[offset : offset + limit]
        preview_count = min(self.max_tool_preview_items, len(sliced))
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
            text = self._to_json_text(summary)
            if len(text) <= self.max_tool_response_chars:
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
