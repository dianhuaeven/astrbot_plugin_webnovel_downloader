from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import register

from .core.download_manager import ExtractionRules
from .plugin_base import JsonlNovelDownloaderPluginBase
from .plugin_support import compat_llm_tool


@register(
    "astrbot_plugin_webnovel_downloader",
    "OpenAI",
    "网文下载器：基于单文件 journal 的纯 Python 网文下载与装订插件，支持断点续传、绝对有序输出与函数工具调用",
    "0.1.0",
    "https://github.com/dianhuaeven/astrbot_plugin_webnovel_downloader",
)
class JsonlNovelDownloaderPlugin(JsonlNovelDownloaderPluginBase):
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
        return await self.handle_novel_fetch_preview(url, encoding, max_chars)

    @compat_llm_tool(name="novel_import_sources")
    async def novel_import_sources(self, event: AstrMessageEvent, source_json: str) -> str:
        """
        导入 Legado/阅读风格书源 JSON。

        Args:
            source_json(string): 单个书源对象、书源数组，或带 sources 字段的 JSON 字符串。
        """
        return await self.handle_novel_import_sources(source_json)

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
        return await self.handle_novel_list_sources(enabled_only, limit, offset)

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
        return await self.handle_novel_enable_source(source_id, enabled)

    @compat_llm_tool(name="novel_remove_source")
    async def novel_remove_source(self, event: AstrMessageEvent, source_id: str) -> str:
        """
        删除一个已导入的书源。

        Args:
            source_id(string): 书源 ID。
        """
        return await self.handle_novel_remove_source(source_id)

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
        return await self.handle_novel_search_books(
            keyword,
            source_ids_json,
            limit,
            include_disabled,
        )

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
        return await self.handle_novel_download_book(
            source_id,
            book_url,
            book_name,
            output_filename,
            auto_assemble,
        )

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
        return await self.handle_novel_resume_book_download(job_id, auto_assemble)

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
        return await self.handle_novel_start_download(
            book_name,
            toc_json,
            content_regex,
            title_regex,
            source_url,
            output_filename,
            encoding,
            auto_assemble,
        )

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
        return await self.handle_novel_resume_download(job_id, auto_assemble)

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
        return await self.handle_novel_download_status(job_id, limit, offset)

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
        return await self.handle_novel_assemble_book(job_id, cleanup_journal)

    @compat_llm_tool(name="novel_list_jobs")
    async def novel_list_jobs(
        self, event: AstrMessageEvent, limit: str = "", offset: str = ""
    ) -> str:
        """
        列出当前插件数据目录下的所有小说下载任务。
        """
        return await self.handle_novel_list_jobs(limit, offset)

    @filter.command("novel_jobs")
    async def novel_jobs_command(self, event):
        yield event.plain_result(await self.render_novel_jobs_command_text())

    @filter.command("novel_sources")
    async def novel_sources_command(self, event):
        yield event.plain_result(await self.render_novel_sources_command_text())


__all__ = ["ExtractionRules", "JsonlNovelDownloaderPlugin"]
