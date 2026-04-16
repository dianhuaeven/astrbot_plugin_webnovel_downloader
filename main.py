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
        self.manager = NovelDownloadManager(
            StarTools.get_data_dir(),
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
        self._running_tasks: dict[str, asyncio.Task] = {}
        logger.info("网文下载器初始化完成")

    @compat_llm_tool(name="novel_fetch_preview")
    async def novel_fetch_preview(
        self, url: str, encoding: str = "", max_chars: int = 0
    ) -> str:
        """
        抓取网页预览，帮助分析目录页或章节页结构。

        Args:
            url(string): 目标网页地址。
            encoding(string): 可选，强制指定编码，例如 utf-8 或 gb18030。
            max_chars(integer): 最多返回多少字符，填 0 表示使用插件默认值。
        """
        preview = await asyncio.to_thread(
            self.manager.fetch_preview,
            url,
            encoding,
            max_chars or None,
        )
        return json.dumps(preview, ensure_ascii=False, indent=2)

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
        auto_assemble: bool = True,
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
            auto_assemble(boolean): 下载完成后是否自动组装 TXT。
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
        await self._ensure_job_running(job_id, auto_assemble)
        status = self.manager.get_status(job_id)
        return self._render_status(status, created=job_info["created"])

    @compat_llm_tool(name="novel_resume_download")
    async def novel_resume_download(self, job_id: str, auto_assemble: bool = True) -> str:
        """
        恢复一个已存在的下载任务，只抓取缺失章节。

        Args:
            job_id(string): 任务 ID。
            auto_assemble(boolean): 下载完成后是否自动组装 TXT。
        """
        await self._ensure_job_running(job_id, auto_assemble)
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
        self, job_id: str, cleanup_journal: bool = False
    ) -> str:
        """
        将一个已下载完成的任务组装成最终 TXT。

        Args:
            job_id(string): 任务 ID。
            cleanup_journal(boolean): 组装完成后是否删除 JSONL journal。
        """
        status = await asyncio.to_thread(
            self.manager.assemble,
            job_id,
            cleanup_journal or self.manager.config.cleanup_journal_after_assemble,
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
