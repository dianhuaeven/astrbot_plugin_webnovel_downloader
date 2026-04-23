from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import register

from .core.download_manager import ExtractionRules
from .plugin_base import JsonlNovelDownloaderPluginBase
from .plugin_support import compat_hidden_tool, compat_llm_tool


@register(
    "astrbot_plugin_webnovel_downloader",
    "Dianhua",
    "网文下载器：基于单文件 journal 的纯 Python 网文下载与装订插件，支持断点续传、绝对有序输出与函数工具调用",
    "0.8.0",
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
        导入一份正文净化规则仓库，供后续 TXT 下载自动清洗广告和杂质内容。

        Args:
            repo_json(string): 净化规则内容，支持仓库 URL、文件路径或原始 JSON/文本。
            repo_name(string): 可选，自定义仓库名称；留空时会尽量从内容里自动识别。
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
        查看当前已导入的正文净化规则仓库，确认哪些清洗规则可用于后续下载。

        Args:
            limit(string): 可选，本次最多返回多少条仓库记录。
            offset(string): 可选，从第几条仓库记录开始返回。
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
        导入一批 Legado/阅读风格书源，并写入本地书源注册表供后续搜索和下载使用。

        Args:
            source_json(string): 书源内容，支持单个书源对象、书源数组、带 sources 字段的 JSON，或这些内容的 URL/文件路径。
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
        查看当前书源清单与可用性摘要，适合在下载前确认哪些书源可参与搜索或下载。

        Args:
            enabled_only(string): 是否只显示已启用书源，支持 true/false/1/0/yes/no。
            limit(string): 可选，本次最多返回多少条书源预览。
            offset(string): 可选，从第几条书源开始返回，支持 0、10、20 这类非负整数。
        """
        return await self.handle_novel_list_sources(enabled_only, limit, offset)

    @compat_llm_tool(name="novel_get_source_detail")
    async def novel_get_source_detail(self, event: AstrMessageEvent, source_id: str) -> str:
        """
        查看单个书源的详细信息，包括静态能力、健康状态、编译后的 profile 和关键规则摘要。

        Args:
            source_id(string): 要查询的书源 ID。
        """
        return await self.handle_novel_get_source_detail(source_id)

    @compat_llm_tool(name="novel_refresh_sources")
    async def novel_refresh_sources(
        self,
        event: AstrMessageEvent,
        source_ids_json: str = "",
        include_disabled: str = "",
    ) -> str:
        """
        将书源重新加入后台健康探测队列，用于刷新存活状态和能力摘要；该工具会立即返回，不等待探测完成。

        Args:
            source_ids_json(string): 可选，JSON 数组或逗号分隔的书源 ID 列表；留空时刷新全部启用书源。
            include_disabled(string): 是否连禁用书源一起加入刷新队列，支持 true/false/1/0/yes/no。
        """
        return await self.handle_novel_refresh_sources(source_ids_json, include_disabled)

    @compat_llm_tool(name="novel_remove_source")
    async def novel_remove_source(self, event: AstrMessageEvent, source_id: str) -> str:
        """
        从本地注册表删除一个已导入书源，适合清理失效、重复或不想继续参与搜索下载的源。

        Args:
            source_id(string): 要删除的书源 ID。
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

    @compat_llm_tool(name="novel_query_candidates")
    async def novel_query_candidates(
        self,
        event: AstrMessageEvent,
        keyword: str,
        author: str = "",
        source_ids_json: str = "",
        limit: str = "",
        offset: str = "",
        include_disabled: str = "",
    ) -> str:
        """
        按书名只查询候选下载源和排序结果，不启动下载任务；可配合 offset 分页查看下一批候选源。

        Args:
            keyword(string): 搜索关键词，通常填写书名。
            author(string): 可选，作者名；填写后会优先展示标题和作者都匹配的候选。
            source_ids_json(string): 可选，只在指定书源范围内查询；支持 JSON 数组或逗号分隔的书源 ID。
            limit(string): 可选，本次返回多少条候选结果。
            offset(string): 可选，从第几条候选结果开始返回，用于手动查看下一批候选。
            include_disabled(string): 是否在查询时包含禁用书源，支持 true/false/1/0/yes/no。
        """
        return await self.handle_novel_query_candidates(
            keyword,
            author,
            source_ids_json,
            limit,
            offset,
            include_disabled,
        )

    @compat_llm_tool(name="novel_probe_status")
    async def novel_probe_status(
        self,
        event: AstrMessageEvent,
        source_ids_json: str = "",
        include_disabled: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        查看后台健康探测的当前进度，以及指定书源范围内的健康状态摘要。

        Args:
            source_ids_json(string): 可选，只查看指定书源；支持 JSON 数组或逗号分隔的书源 ID。留空时统计全部已启用书源。
            include_disabled(string): 是否把禁用书源也纳入统计，支持 true/false/1/0/yes/no。
            limit(string): 可选，本次最多返回多少条书源健康摘要。
            offset(string): 可选，从第几条书源开始返回，用于分页查看健康状态。
        """
        return await self.handle_novel_probe_status(
            source_ids_json,
            include_disabled,
            limit,
            offset,
        )

    @compat_llm_tool(name="novel_inspect_source_book")
    async def novel_inspect_source_book(
        self,
        event: AstrMessageEvent,
        source_id: str,
        book_url: str,
        book_name: str = "",
    ) -> str:
        """
        在不创建任务的前提下，查询某个书源对指定书籍详情页的预检和正文抽样结果。

        Args:
            source_id(string): 书源 ID。
            book_url(string): 书籍详情页地址。
            book_name(string): 可选，手动指定书名，便于预检失败时保留上下文。
        """
        return await self.handle_novel_inspect_source_book(
            source_id,
            book_url,
            book_name,
        )

    @compat_hidden_tool()
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
        兼容入口：按书名返回候选下载源摘要，但不再启动下载任务。

        Args:
            keyword(string): 搜索关键词，通常填写书名。
            author(string): 可选，作者名；填写后会优先展示标题和作者都精确匹配的候选。
            source_ids_json(string): 可选，只在指定书源范围内搜索；支持 JSON 数组或逗号分隔的书源 ID。
            search_limit(string): 可选，本次搜索阶段最多保留多少条候选结果。
            attempt_limit(string): 兼容保留参数，当前已忽略。
            output_filename(string): 兼容保留参数，当前已忽略。
            auto_assemble(string): 兼容保留参数，当前已忽略。
            include_disabled(string): 是否在搜索时包含禁用书源，支持 true/false/1/0/yes/no。
        """
        del attempt_limit, output_filename, auto_assemble
        return await self.handle_novel_prepare_download(
            keyword,
            author,
            source_ids_json,
            search_limit,
            include_disabled,
        )

    @compat_llm_tool(name="novel_download_source_book")
    async def novel_download_source_book(
        self,
        event: AstrMessageEvent,
        source_id: str,
        book_url: str,
        book_name: str = "",
        author: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
    ) -> str:
        """
        直接指定某个书源和书籍详情页启动下载；必须先确认标题和作者精确匹配。

        Args:
            source_id(string): 书源 ID，通常来自 novel_query_candidates 或 novel_get_source_detail。
            book_url(string): 书籍详情页地址。
            book_name(string): 目标书名，必须与候选结果中的标题精确一致。
            author(string): 目标作者，必须与候选结果中的作者精确一致。
            output_filename(string): 可选，自定义输出 TXT 文件名。
            auto_assemble(string): 是否在下载完成后自动组装 TXT，支持 true/false/1/0/yes/no。
        """
        if not str(book_name or "").strip() or not str(author or "").strip():
            raise ValueError(
                "为避免误下错书，novel_download_source_book 现在要求同时提供精确的 book_name 和 author。"
            )
        return await self.handle_novel_download_book(
            source_id,
            book_url,
            book_name,
            output_filename,
            auto_assemble,
            book_name,
            author,
        )

    @compat_llm_tool(name="novel_read_search_results")
    async def novel_read_search_results(
        self,
        event: AstrMessageEvent,
        search_id: str,
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        分页查看某次缓存搜索的原始结果，方便在自动候选之外手动检查更多书源命中。

        Args:
            search_id(string): 搜索缓存 ID，通常来自 novel_query_candidates 或其他搜索相关工具的返回。
            limit(string): 可选，本次最多返回多少条原始搜索结果。
            offset(string): 可选，从第几条结果开始返回。
        """
        return await self.handle_novel_get_search_results(search_id, limit, offset)

    @compat_hidden_tool()
    async def novel_download_cached_result(
        self,
        event: AstrMessageEvent,
        search_id: str,
        result_index: str,
        output_filename: str = "",
        auto_assemble: str = "true",
    ) -> str:
        """
        兼容入口：该能力不再对 LLM 暴露，建议改用候选查询后再指定源下载。

        Args:
            search_id(string): 搜索缓存 ID。
            result_index(string): 搜索结果索引，通常来自 novel_read_search_results 返回的 result_index。
            output_filename(string): 可选，自定义输出 TXT 文件名。
            auto_assemble(string): 是否在下载完成后自动组装 TXT，支持 true/false/1/0/yes/no。
        """
        return await self.handle_novel_download_search_result(
            search_id,
            result_index,
            output_filename,
            auto_assemble,
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
        兼容入口：返回候选源摘要，不再启动自动下载任务。

        Args:
            keyword(string): 搜索关键词，通常是书名。
            author(string): 可选，作者名；传入后会优先展示标题和作者都精确匹配的候选。
            source_ids_json(string): 可选，JSON 数组或逗号分隔的书源 ID 列表。
            search_limit(string): 可选，本次搜索最多保留多少条候选结果。
            attempt_limit(string): 兼容保留参数，当前已忽略。
            output_filename(string): 兼容保留参数，当前已忽略。
            auto_assemble(string): 兼容保留参数，当前已忽略。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
        """
        del attempt_limit, output_filename, auto_assemble
        return await self.handle_novel_prepare_download(
            keyword,
            author,
            source_ids_json,
            search_limit,
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
        查询下载任务的状态、进度和输出文件信息；未传 job_id 时返回任务列表摘要。

        Args:
            job_id(string): 可选，指定要查看的下载任务 ID。
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
        del search_id, result_index, output_filename, auto_assemble
        yield event.plain_result(
            "为避免误下错书，`novel_download_result` 已停用；请先用 `novel_auto` 确认精确候选，再用 `novel_download <source_id> <book_url> [book_name]` 下载。"
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
            await self.handle_novel_prepare_download(
                keyword,
                author,
                source_ids_json,
                search_limit,
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
