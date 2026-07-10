from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import register

from .base import JsonlNovelDownloaderPluginBase
from .core.download_manager import ExtractionRules
from .support import compat_admin_only, compat_llm_tool, compat_on_llm_request


@register(
    "astrbot_plugin_webnovel_downloader",
    "Dianhua",
    "网文下载器：基于书源聚合、预检择优和单文件 journal 的纯 Python 网文下载与装订插件",
    "1.0.0",
    "https://github.com/dianhuaeven/astrbot_plugin_webnovel_downloader",
)
class JsonlNovelDownloaderPlugin(JsonlNovelDownloaderPluginBase):
    @compat_on_llm_request()
    async def webnovel_quiet_tool_policy(self, event, req) -> None:
        del event
        policy = (
            "Webnovel tool-call exception: when using any `webnovel_*` tool, "
            "call it without a pre-tool narration or progress message. Do not say "
            "that you are about to search, inspect, download, check status, or send "
            "a file. Speak before a tool call only when the user must provide missing "
            "information or choose between genuinely ambiguous works. Do not poll a "
            "download unless the user explicitly asks for its status. After the final "
            "tool action, send at most one concise result message."
        )
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if policy not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{policy}"

    @compat_llm_tool(name="webnovel_search_books")
    async def webnovel_search_books(
        self,
        event: AstrMessageEvent,
        keyword: str,
        author: str = "",
        limit: str = "",
        include_disabled: str = "",
    ) -> str:
        """
        为一次已经确认目标的小说下载查找候选书源，并按“同名同作者”聚合结果。

        这是耗时较长的下载准备步骤，不是通用搜索、推荐、探索或可用性检查工具。
        仅当用户在当前请求中明确要求下载完整小说，并已确认准确书名后调用；
        同名作品可能混淆时，必须先向用户确认作者。搜索结果应紧接着用于当前下载。

        Args:
            keyword(string): 已确认下载目标的准确书名；不得填写题材、剧情描述或推荐关键词。
            author(string): 可选，已确认的作者名；同名作品有歧义时应在调用搜索前向用户确认。
            limit(string): 可选，本次最多返回多少个聚合候选组。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
        """
        return await self.handle_webnovel_search_books(
            keyword,
            author,
            limit,
            include_disabled,
            event=event,
        )

    @compat_llm_tool(name="webnovel_download_book")
    async def webnovel_download_book(
        self,
        event: AstrMessageEvent,
        search_id: str,
        group_index: str,
        attempt_limit: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
        skip_source_ids: str = "",
    ) -> str:
        """
        从一次聚合搜索结果中选择一本书下载；会在组内多个书源中预检择优，并且只创建一个正式下载任务。

        Args:
            search_id(string): 来自 webnovel_search_books 返回的搜索缓存 ID。
            group_index(string): 搜索结果中的聚合候选组序号。
            attempt_limit(string): 可选，最多尝试多少个候选书源。
            output_filename(string): 可选，自定义输出 TXT 文件名。
            auto_assemble(string): 是否下载完成后自动装订 TXT，支持 true/false/1/0/yes/no。
            skip_source_ids(string): 可选，本次跳过的书源 ID；支持单个、逗号分隔、中文逗号、换行或 JSON 数组。
        """
        kwargs = {"event": event} if event is not None else {}
        return await self.handle_webnovel_download_book(
            search_id,
            group_index,
            attempt_limit,
            output_filename,
            auto_assemble,
            skip_source_ids,
            **kwargs,
        )

    @compat_llm_tool(name="webnovel_download_status")
    async def webnovel_download_status(
        self,
        event: AstrMessageEvent,
        job_id: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        查询下载任务状态；不传 job_id 时列出任务摘要。

        Args:
            job_id(string): 可选，要查询的任务 ID。
            limit(string): 可选，列出任务时最多返回多少条。
            offset(string): 可选，列出任务时从第几条开始返回。
        """
        return await self.handle_webnovel_download_status(
            job_id, limit, offset, event=event
        )

    @compat_llm_tool(name="webnovel_send_book")
    async def webnovel_send_book(
        self,
        event: AstrMessageEvent,
        job_id: str = "",
        book_name: str = "",
        author: str = "",
    ) -> str:
        """
        把插件缓存中已经下载完成的小说文件发送到当前会话。

        这是发送小说文件的唯一工具，不依赖电脑使用能力，也不要求管理员权限。
        用户要求下载明确作品时应优先静默调用本工具检查并发送缓存；只有返回
        no_cache 后才能搜索和下载。用户说“发给我”时也直接调用，不要先解释。
        本工具不会搜索书源、启动下载、读取任意宿主机文件或发送非小说文件。

        Args:
            job_id(string): 可选，已完成下载任务的 ID；已知时优先填写。
            book_name(string): 可选，要发送的准确书名；job_id 未知时填写。
            author(string): 可选，作者名；同名作品有歧义时必须填写。
        """
        return await self.handle_webnovel_send_book(
            job_id,
            book_name,
            author,
            event=event,
        )

    @compat_admin_only()
    @compat_llm_tool(name="webnovel_import_sources")
    async def webnovel_import_sources(
        self,
        event: AstrMessageEvent,
        source_json: str,
    ) -> str:
        """
        导入 Legado/阅读风格书源，写入本地书源注册表。

        Args:
            source_json(string): 书源内容，支持 URL、文件路径、单个书源对象、书源数组或带 sources 字段的 JSON。
        """
        return await self.handle_webnovel_import_sources(source_json)

    @compat_llm_tool(name="webnovel_list_sources")
    async def webnovel_list_sources(
        self,
        event: AstrMessageEvent,
        enabled_only: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        查看书源列表、静态能力和健康摘要。

        Args:
            enabled_only(string): 是否只显示启用书源，支持 true/false/1/0/yes/no。
            limit(string): 可选，本次最多返回多少条。
            offset(string): 可选，从第几条开始返回。
        """
        return await self.handle_webnovel_list_sources(enabled_only, limit, offset)

    @compat_admin_only()
    @compat_llm_tool(name="webnovel_refresh_sources")
    async def webnovel_refresh_sources(
        self,
        event: AstrMessageEvent,
        source_ids_json: str = "",
        include_disabled: str = "",
    ) -> str:
        """
        将书源加入后台健康探测队列；立即返回，不等待探测完成。

        Args:
            source_ids_json(string): 可选，JSON 数组或逗号分隔的书源 ID 列表；留空时刷新全部启用书源。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
        """
        return await self.handle_webnovel_refresh_sources(
            source_ids_json,
            include_disabled,
        )

    @compat_llm_tool(name="webnovel_probe_status")
    async def webnovel_probe_status(
        self,
        event: AstrMessageEvent,
        source_ids_json: str = "",
        include_disabled: str = "",
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        查看后台健康探测进度，以及指定书源范围内的健康状态。

        Args:
            source_ids_json(string): 可选，只查看指定书源；支持 JSON 数组或逗号分隔的书源 ID。
            include_disabled(string): 是否包含禁用书源，支持 true/false/1/0/yes/no。
            limit(string): 可选，本次最多返回多少条健康摘要。
            offset(string): 可选，从第几条开始返回。
        """
        return await self.handle_webnovel_probe_status(
            source_ids_json,
            include_disabled,
            limit,
            offset,
        )

    @compat_admin_only()
    @compat_llm_tool(name="webnovel_import_clean_rules")
    async def webnovel_import_clean_rules(
        self,
        event: AstrMessageEvent,
        repo_json: str,
        repo_name: str = "",
    ) -> str:
        """
        导入正文净化规则仓库，用于下载后清理广告和杂质文本。

        Args:
            repo_json(string): 净化规则内容，支持 URL、文件路径或原始 JSON/文本。
            repo_name(string): 可选，自定义仓库名称。
        """
        return await self.handle_webnovel_import_clean_rules(repo_json, repo_name)

    @compat_llm_tool(name="webnovel_list_clean_rules")
    async def webnovel_list_clean_rules(
        self,
        event: AstrMessageEvent,
        limit: str = "",
        offset: str = "",
    ) -> str:
        """
        查看已导入的正文净化规则仓库。

        Args:
            limit(string): 可选，本次最多返回多少条仓库记录。
            offset(string): 可选，从第几条仓库记录开始返回。
        """
        return await self.handle_webnovel_list_clean_rules(limit, offset)

    @filter.command("novel_jobs")
    async def novel_jobs_command(self, event):
        yield event.plain_result(await self.handle_novel_list_jobs(event=event))

    @filter.command("novel_sources")
    async def novel_sources_command(self, event):
        yield event.plain_result(await self.handle_novel_list_sources())

    @compat_admin_only()
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

    @compat_admin_only()
    @filter.command("novel_import")
    async def novel_import_command(self, event, source_json: str):
        yield event.plain_result(await self.handle_novel_import_sources(source_json))

    @compat_admin_only()
    @filter.command("novel_import_clean")
    async def novel_import_clean_command(
        self,
        event,
        repo_json: str,
        repo_name: str = "",
    ):
        yield event.plain_result(
            await self.handle_novel_import_clean_rules(repo_json, repo_name)
        )

    @filter.command("novel_clean_rules")
    async def novel_clean_rules_command(self, event, limit: str = "", offset: str = ""):
        yield event.plain_result(
            await self.handle_novel_list_clean_rules(limit, offset)
        )

    @filter.command("novel_search")
    async def novel_search_command(
        self,
        event,
        keyword: str,
        author: str = "",
        limit: str = "",
        include_disabled: str = "",
    ):
        yield event.plain_result(
            await self.handle_webnovel_search_books(
                keyword,
                author,
                limit,
                include_disabled,
            )
        )

    @filter.command("novel_auto")
    async def novel_auto_command(
        self,
        event,
        keyword: str,
        author: str = "",
        limit: str = "",
        include_disabled: str = "",
    ):
        yield event.plain_result(
            await self.handle_webnovel_search_books(
                keyword,
                author,
                limit,
                include_disabled,
            )
        )

    @filter.command("novel_download")
    async def novel_download_command(
        self,
        event,
        search_id: str,
        group_index: str,
        attempt_limit: str = "",
        output_filename: str = "",
        auto_assemble: str = "true",
        skip_source_ids: str = "",
    ):
        yield event.plain_result(
            await self.handle_webnovel_download_book(
                search_id,
                group_index,
                attempt_limit,
                output_filename,
                auto_assemble,
                skip_source_ids,
                event=event,
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
        yield event.plain_result(
            await self.handle_novel_download_status(job_id, limit, offset, event=event)
        )

    @compat_admin_only()
    @filter.command("novel_remove")
    async def novel_remove_command(self, event, source_id: str):
        yield event.plain_result(await self.handle_novel_remove_source(source_id))

    @compat_admin_only()
    @filter.command("novel_preview")
    async def novel_preview_command(
        self,
        event,
        url: str,
        encoding: str = "",
        max_chars: str = "",
    ):
        yield event.plain_result(
            await self.handle_novel_fetch_preview(url, encoding, max_chars)
        )


__all__ = ["ExtractionRules", "JsonlNovelDownloaderPlugin"]
