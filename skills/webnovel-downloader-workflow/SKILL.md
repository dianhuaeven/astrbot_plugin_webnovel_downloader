---
name: webnovel-downloader-workflow
description: 使用 AstrBot 网文下载插件处理“导入书源、刷新并查看书源探测状态、查询候选源、检查单源可下载性、按书名或缓存结果下载小说、查询下载进度”等任务。Use when users want to download web novels through the plugin, manage Legado sources, decide between high-level auto download and candidate/probe/cache branches, or need an up-to-date view of which novel tools are exposed.
---

# Webnovel Downloader Workflow

在用户想通过本插件导入 Legado 书源、下载小说或查看任务进度时使用本技能。

## Branch Workflow

1. 如果用户提供了书源 URL、书源文件路径或书源 JSON，先用 `novel_import_sources` 导入书源。
2. 如果用户提到广告多、正文脏、要加净化规则，用 `novel_import_clean_rules` 或 `novel_list_clean_rules`。
3. 如果用户只是想看现有书源或确认启用状态，用 `novel_list_sources`；如果用户已经点名某个 `source_id`，或想看健康状态、编译后的 profile、关键规则摘要，用 `novel_get_source_detail`。
4. 如果用户刚导入完书源、怀疑健康度过期、想刷新能力摘要，先用 `novel_refresh_sources`。
   - 这是后台异步探测，只表示“已经入队”，不要等待探测全部完成再继续聊天。
   - 当用户需要确认探测有没有跑完、某批书源当前健康状况如何时，再用 `novel_probe_status` 查看队列、活跃探测和分页健康摘要。
5. 进入下载分支时，按下面的决策走：
   - 直接走 `novel_download`：用户只是想“把这本书下载下来”，且不需要先人工比较候选源时。
   - 先走 `novel_query_candidates`：书名明显有歧义、用户想先看候选源、上一次高层下载失败后要手动挑源、或需要分页查看更多候选时。
   - 先走 `novel_inspect_source_book`：用户已经拿到某个候选的 `source_id + book_url`，但想先确认目录预检、正文抽样或“这个源能不能稳定下”时。
   - 直接走 `novel_download_source_book`：上下文已经有明确的 `source_id + book_url`，不需要再重新搜书时。
   - 走缓存续下链路：如果上下文已经有 `search_id`，先用 `novel_read_search_results` 分页查看缓存搜索结果；选中某条结果后，用 `novel_download_cached_result` 按 `result_index` 继续下载，而不是重新调用高层 `novel_download`。
6. 任务创建后，用 `novel_download_status` 汇报进度和结果。
   - 轮询要克制，不要无意义地高频重复查询。

## Tool Selection Rules

- 默认优先使用高层确定性工具 `novel_download`，不要在对话里模拟“一个个试书源”。
- 当用户只要求结果，不要求看候选、比较书源或复用旧搜索缓存时，直接走 `novel_download`。
- 当用户想看候选、书名歧义明显、需要人工挑源、或高层下载失败后要控制换源过程时，先走 `novel_query_candidates`。
- 拿到候选后，如果用户想先做只读确认，用 `novel_inspect_source_book`；如果已经决定要用某个源，直接用 `novel_download_source_book`。
- 当上下文里已经有 `search_id`，优先复用 `novel_read_search_results` 和 `novel_download_cached_result`，避免为了翻页、换源或重试又触发一次全量搜索。
- `novel_get_source_detail` 适合回答“这个源为什么不可用”“这个源支持哪些能力”“这个源最近探测结果怎样”这类问题。
- 只有在书名明显歧义时，再向用户补问 `author`；否则可直接开下。
- 只有在用户明确要求限定书源，或上下文已经锁定一小批源时，才传 `source_ids_json`。
- `novel_refresh_sources` 是异步队列工具；返回后应告诉用户刷新已经开始，而不是假装探测已完成。
- `novel_probe_status` 用来查看探测是否还在跑、队列有多长、某批书源当前健康摘要是什么，不要把它当成下载状态工具。
- 如果导入/列源结果提示 `JS 规则`、`jsLib`、`loginUrl/loginUi` 或类似限制，要明确说明该插件偏向纯 Python 静态书源，这类源成功率会低。
- 如果下载失败一次，先总结失败原因和下一步建议；需要改走候选筛选、缓存续下或指定单源时再切分支，不要机械地用同一参数连续重试。

## Available LLM Tools

- `novel_import_sources`: 导入 Legado/阅读书源。
- `novel_import_clean_rules`: 导入正文净化规则仓库。
- `novel_list_sources`: 查看书源清单和能力摘要。
- `novel_get_source_detail`: 查看单个书源的详细信息，包括健康状态和编译后的 profile。
- `novel_refresh_sources`: 将书源重新加入后台健康探测队列。
- `novel_probe_status`: 查看后台健康探测进度、队列状态和分页健康摘要。
- `novel_list_clean_rules`: 查看已导入的净化规则仓库。
- `novel_remove_source`: 删除已导入书源。
- `novel_query_candidates`: 按书名只查询候选下载源和排序结果，不启动下载。
- `novel_inspect_source_book`: 针对指定 `source_id + book_url` 执行预检和正文抽样，不创建任务。
- `novel_download`: 按书名自动搜书、筛选候选源并启动下载任务。
- `novel_download_source_book`: 指定 `source_id + book_url` 启动下载。
- `novel_read_search_results`: 按 `search_id` 分页查看缓存搜索结果。
- `novel_download_cached_result`: 按 `search_id + result_index` 从缓存搜索结果继续下载。
- `novel_download_status`: 查询下载任务进度、状态和结果摘要。

## Boundaries

- 当前对模型已经开放了受控的候选查询、缓存结果读取、`source_id + book_url` 直连下载和缓存结果续下能力；不要再说“这些能力未暴露”。
- 仍主要保留给人工命令或内部调试的能力包括页面预览、手工 regex 下载、底层任务恢复和其他更自由的调试链路。
- 不要承诺支持需要浏览器渲染、验证码、登录态或复杂 JS 规则的书源。

## Response Style

- 下载前：简短说明将使用的书名、作者，以及当前选择的是高层下载、候选查询、探测状态查看还是缓存续下分支。
- 下载中：只汇报关键进度，不回灌大段 JSON。
- 下载失败：给出失败摘要和最可能的下一步，例如补作者、刷新书源、查看探测状态、先查候选、改走缓存续下或换静态源。
- 下载成功：明确告知任务已完成，并引用工具返回的任务信息或输出文件信息。

## Reference

需要精确查看当前可用工具、参数和人工命令映射时，读取 [references/tool-catalog.md](references/tool-catalog.md)。
