from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import register

from .core.download_manager import ExtractionRules
from .plugin_base import JsonlNovelDownloaderPluginBase
from .plugin_support import compat_hidden_tool, compat_llm_tool


@register(
    "astrbot_plugin_webnovel_downloader",
    "OpenAI",
    "网文下载器：基于单文件 journal 的纯 Python 网文下载与装订插件，支持断点续传、绝对有序输出与函数工具调用",
    "0.1.0",
    "https://github.com/dianhuaeven/astrbot_plugin_webnovel_downloader",
)
class JsonlNovelDownloaderPlugin(JsonlNovelDownloaderPluginBase):
    @compat_llm_tool(name="novel_import_clean_rules")
    async def novel_import_clean_rules(
        self,
        event: AstrMessageEvent,
        repo_json: str,
        repo_name: str = "",
    ) -> str:
        """
        导入一个正文净化规则仓库，并在后续下载正文时自动应用。

        Args:
            repo_json(string): 净化规则 JSON/文本，支持 URL、文件路径或原始内容。
            repo_name(string): 可选，手动指定这份净化规则仓库的名称。
        """
        return await self.handle_novel_import_clean_rules(repo_json, repo_name)

    @compat_llm_tool(name="novel_list_clean_rules")
    async def novel_list_clean_rules(
        self,
        event: AstrMessageEvent,
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        列出已导入的正文净化规则仓库。

        Args:
            limit(string): 可选，本次最多返回多少条规则仓库记录。
            offset(string): 可选，从第几条规则仓库记录开始返回。
        """
        return await self.handle_novel_list_clean_rules(limit, offset)

    @compat_hidden_tool()
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

    @compat_llm_tool(name="novel_refresh_sources")
    async def novel_refresh_sources(
        self,
        event: AstrMessageEvent,
        source_ids_json: str = "",
        include_disabled: str = "",
    ) -> str:
        """
        将指定书源重新加入后台健康探测队列，不等待探测完成。

        Args:
            source_ids_json(string): 可选，JSON 数组或逗号分隔的书源 ID 列表；留空时刷新全部启用书源。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
        """
        return await self.handle_novel_refresh_sources(source_ids_json, include_disabled)

    @compat_llm_tool(name="novel_remove_source")
    async def novel_remove_source(self, event: AstrMessageEvent, source_id: str) -> str:
        """
        删除一个已导入的书源。

        Args:
            source_id(string): 书源 ID。
        """
        return await self.handle_novel_remove_source(source_id)

    @compat_hidden_tool()
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

    @compat_hidden_tool()
    async def novel_list_searches(
        self, event: AstrMessageEvent, limit: str = "", offset: str = ""
    ) -> str:
        """
        列出最近缓存的搜索记录。

        Args:
            limit(string): 可选，本次最多返回多少条搜索记录。
            offset(string): 可选，从第几条搜索记录开始返回。
        """
        return await self.handle_novel_list_searches(limit, offset)

    @compat_hidden_tool()
    async def novel_get_search_results(
        self,
        event: AstrMessageEvent,
        search_id: str,
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        查看某次缓存搜索的结果列表。

        Args:
            search_id(string): 搜索缓存 ID，通常来自 novel_search_books 的返回。
            limit(string): 可选，本次最多返回多少条结果。
            offset(string): 可选，从第几条结果开始返回。
        """
        return await self.handle_novel_get_search_results(search_id, limit, offset)

    @compat_llm_tool(name="novel_download")
    async def novel_download(
        self,
        event: AstrMessageEvent,
        keyword: str,
        author: str = "",
        source_ids_json: str = "",
        search_limit: str = "",
        attempt_limit: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
        include_disabled: str = "",
    ) -> str:
        """
        自动搜书、择优挑选候选源，并在 Python 侧完成预检回退后启动下载任务。

        Args:
            keyword(string): 搜索关键词，通常是书名。
            author(string): 可选，作者名；传入后会优先选择标题和作者都匹配的候选。
            source_ids_json(string): 可选，JSON 数组或逗号分隔的书源 ID 列表。
            search_limit(string): 可选，本次搜索最多保留多少条候选结果。
            attempt_limit(string): 可选，最多尝试多少个候选源做目录预检。
            output_filename(string): 可选，自定义输出 TXT 文件名。
            auto_assemble(string): 是否自动组装 TXT，支持 true/false/1/0/yes/no。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
        """
        return await self.handle_novel_auto_download(
            keyword,
            author,
            source_ids_json,
            search_limit,
            attempt_limit,
            output_filename,
            auto_assemble,
            include_disabled,
        )

    @compat_hidden_tool()
    async def novel_auto_download(
        self,
        event: AstrMessageEvent,
        keyword: str,
        author: str = "",
        source_ids_json: str = "",
        search_limit: str = "",
        attempt_limit: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
        include_disabled: str = "",
    ) -> str:
        """
        自动搜书、择优挑选候选源，并在 Python 侧完成预检回退后启动下载任务。

        Args:
            keyword(string): 搜索关键词，通常是书名。
            author(string): 可选，作者名；传入后会优先选择标题和作者都匹配的候选。
            source_ids_json(string): 可选，JSON 数组或逗号分隔的书源 ID 列表。
            search_limit(string): 可选，本次搜索最多保留多少条候选结果。
            attempt_limit(string): 可选，最多尝试多少个候选源做目录预检。
            output_filename(string): 可选，自定义输出 TXT 文件名。
            auto_assemble(string): 是否自动组装 TXT，支持 true/false/1/0/yes/no。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
        """
        return await self.handle_novel_auto_download(
            keyword,
            author,
            source_ids_json,
            search_limit,
            attempt_limit,
            output_filename,
            auto_assemble,
            include_disabled,
        )

    @compat_hidden_tool()
    async def novel_download_search_result(
        self,
        event: AstrMessageEvent,
        search_id: str,
        result_index: str,
        output_filename: str = "",
        auto_assemble: str = "true",
    ) -> str:
        """
        直接基于缓存搜索结果中的某一项发起下载。

        Args:
            search_id(string): 搜索缓存 ID，通常来自 novel_search_books 的返回。
            result_index(string): 结果索引，通常来自 novel_search_books 或 novel_get_search_results 返回的 result_index。
            output_filename(string): 可选，自定义输出 TXT 文件名。
            auto_assemble(string): 是否自动组装 TXT，支持 true/false/1/0/yes/no。
        """
        return await self.handle_novel_download_search_result(
            search_id,
            result_index,
            output_filename,
            auto_assemble,
        )

    @compat_hidden_tool()
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

    @compat_hidden_tool()
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

    @compat_hidden_tool()
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

    @compat_hidden_tool()
    async def novel_list_jobs(
        self, event: AstrMessageEvent, limit: str = "", offset: str = ""
    ) -> str:
        """
        列出当前插件数据目录下的所有小说下载任务。
        """
        return await self.handle_novel_list_jobs(limit, offset)

    @filter.command("novel_jobs")
    async def novel_jobs_command(self, event):
        yield event.plain_result(await self.handle_novel_list_jobs())

    @filter.command("novel_sources")
    async def novel_sources_command(self, event):
        yield event.plain_result(await self.handle_novel_list_sources())

    @filter.command("novel_refresh")
    async def novel_refresh_command(
        self,
        event,
        source_ids_json: str = "",
        include_disabled: str = "",
    ):
        yield event.plain_result(
            await self.handle_novel_refresh_sources(
                source_ids_json,
                include_disabled,
            )
        )

    @filter.command("novel_import")
    async def novel_import_command(self, event, source_json: str):
        yield event.plain_result(await self.handle_novel_import_sources(source_json))

    @filter.command("novel_import_clean")
    async def novel_import_clean_command(self, event, repo_json: str, repo_name: str = ""):
        yield event.plain_result(await self.handle_novel_import_clean_rules(repo_json, repo_name))

    @filter.command("novel_clean_rules")
    async def novel_clean_rules_command(self, event, limit: str = "", offset: str = ""):
        yield event.plain_result(await self.handle_novel_list_clean_rules(limit, offset))

    @filter.command("novel_search")
    async def novel_search_command(
        self,
        event,
        keyword: str,
        source_ids_json: str = "",
        limit: str = "",
        include_disabled: str = "",
    ):
        yield event.plain_result(
            await self.handle_novel_search_books(
                keyword,
                source_ids_json,
                limit,
                include_disabled,
            )
        )

    @filter.command("novel_searches")
    async def novel_searches_command(self, event, limit: str = "", offset: str = ""):
        yield event.plain_result(await self.handle_novel_list_searches(limit, offset))

    @filter.command("novel_search_results")
    async def novel_search_results_command(
        self,
        event,
        search_id: str,
        limit: str = "",
        offset: str = "",
    ):
        yield event.plain_result(await self.handle_novel_get_search_results(search_id, limit, offset))

    @filter.command("novel_download_result")
    async def novel_download_result_command(
        self,
        event,
        search_id: str,
        result_index: str,
        output_filename: str = "",
        auto_assemble: str = "true",
    ):
        yield event.plain_result(
            await self.handle_novel_download_search_result(
                search_id,
                result_index,
                output_filename,
                auto_assemble,
            )
        )

    @filter.command("novel_auto")
    async def novel_auto_command(
        self,
        event,
        keyword: str,
        author: str = "",
        source_ids_json: str = "",
        search_limit: str = "",
        attempt_limit: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
        include_disabled: str = "",
    ):
        yield event.plain_result(
            await self.handle_novel_auto_download(
                keyword,
                author,
                source_ids_json,
                search_limit,
                attempt_limit,
                output_filename,
                auto_assemble,
                include_disabled,
            )
        )

    @filter.command("novel_download")
    async def novel_download_command(
        self,
        event,
        source_id: str,
        book_url: str,
        book_name: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
    ):
        yield event.plain_result(
            await self.handle_novel_download_book(
                source_id,
                book_url,
                book_name,
                output_filename,
                auto_assemble,
            )
        )

    @filter.command("novel_status")
    async def novel_status_command(
        self,
        event,
        job_id: str = "",
        limit: str = "",
        offset: str = "",
    ):
        yield event.plain_result(await self.handle_novel_download_status(job_id, limit, offset))

    @filter.command("novel_remove")
    async def novel_remove_command(self, event, source_id: str):
        yield event.plain_result(await self.handle_novel_remove_source(source_id))

    @filter.command("novel_preview")
    async def novel_preview_command(
        self,
        event,
        url: str,
        encoding: str = "",
        max_chars: str = "",
    ):
        yield event.plain_result(await self.handle_novel_fetch_preview(url, encoding, max_chars))


__all__ = ["ExtractionRules", "JsonlNovelDownloaderPlugin"]
