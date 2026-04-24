---
name: webnovel-downloader-workflow
description: 使用 AstrBot 网文下载插件处理“导入书源、刷新并查看书源探测状态、查询候选源、检查单源可下载性、按精确书名和作者指定源下载、查询下载进度”等任务。Use when users want to download web novels through the plugin, manage Legado sources, follow the current candidate-first workflow, or need an up-to-date view of which novel tools are exposed.
---

# Webnovel Downloader Workflow

在用户想通过本插件导入 Legado 书源、下载小说或查看任务进度时使用本技能。

## Branch Workflow

0. 先判断目标是“给沙箱用文件”还是“把结果发给用户”。
   - 如果当前运行在沙箱里，而用户想让你读取宿主机上的本地文件，先使用 `astrbot_upload_file` 把文件上传到沙箱的 `/workspace`。
   - 如果你已经拿到了下载结果文件，且目标是把小说直接交付给当前请求者，优先使用 `send_message_to_user` 发送 `file` 消息，而不是先上传进沙箱再绕一圈。
   - `astrbot_upload_file` 只接收宿主机绝对路径，并会把文件放到沙箱工作区根目录，文件名默认取 basename。
   - 上传完成后，再把 `/workspace/<filename>` 作为书源文件、净化规则文件或其他本地输入路径传给本插件工具。
   - `send_message_to_user` 支持直接发送 `file` 类型消息；当插件已经返回宿主机上的 `output_path` 时，可直接拿这个路径发给用户。
1. 如果用户提供了书源 URL、书源文件路径或书源 JSON，先用 `novel_import_sources` 导入书源。
2. 如果用户提到广告多、正文脏、要加净化规则，用 `novel_import_clean_rules` 或 `novel_list_clean_rules`。
3. 如果用户只是想看现有书源或确认启用状态，用 `novel_list_sources`；如果用户已经点名某个 `source_id`，或想看健康状态、编译后的 profile、关键规则摘要，用 `novel_get_source_detail`。
4. 如果用户刚导入完书源、怀疑健康度过期、想刷新能力摘要，先用 `novel_refresh_sources`。
   - 这是后台异步探测，只表示“已经入队”，不要等待探测全部完成再继续聊天。
   - 当用户需要确认探测有没有跑完、某批书源当前健康状况如何时，再用 `novel_probe_status` 查看队列、活跃探测和分页健康摘要。
5. 进入下载分支时，按下面的决策走：
   - 默认先走 `novel_query_candidates`：现在不再让 LLM 直接一键模糊下载；先查候选，再决定用哪个源。
   - 先走 `novel_inspect_source_book`：用户已经拿到某个候选的 `source_id + book_url`，但想先确认目录预检、正文抽样或“这个源能不能稳定下”时。
   - 直接走 `novel_download_source_book`：上下文已经有明确的 `source_id + book_url + book_name + author`，并且标题和作者都已确认精确匹配时。
   - 读缓存搜索结果：如果上下文已经有 `search_id`，先用 `novel_read_search_results` 分页查看缓存搜索结果；如果看到了更合适的候选，再改走 `novel_download_source_book`。
6. 任务创建后，用 `novel_download_status` 汇报进度和结果。
   - 轮询要克制，不要无意义地高频重复查询。

## Tool Selection Rules

- 目标是“让沙箱读取宿主机文件”时，用 `astrbot_upload_file`；目标是“把现成结果文件发给用户”时，用 `send_message_to_user`。
- 默认优先使用 `novel_query_candidates`，不要在对话里模拟“一个个试书源”。
- 当前 LLM 不再直接暴露高层一键下载入口；“把这本书下载下来”也应先走 `novel_query_candidates`，再走 `novel_download_source_book`。
- 当用户想看候选、书名歧义明显、需要人工挑源、或上一次下载失败后要控制换源过程时，先走 `novel_query_candidates`。
- 拿到候选后，如果用户想先做只读确认，用 `novel_inspect_source_book`；如果已经决定要用某个源，直接用 `novel_download_source_book`。
- 当上下文里已经有 `search_id`，优先复用 `novel_read_search_results` 查看更多原始搜索结果，避免为了翻页、换源或重试又触发一次全量搜索。
- `novel_download_source_book` 现在要求同时提供精确的 `book_name + author`；这两个值应直接取自候选结果，不要自行改写。
- `novel_get_source_detail` 适合回答“这个源为什么不可用”“这个源支持哪些能力”“这个源最近探测结果怎样”这类问题。
- 只有在书名明显歧义、或候选里同名作品很多时，再向用户补问 `author`；如果已经从候选里拿到作者，就直接沿用候选值。
- 只有在用户明确要求限定书源，或上下文已经锁定一小批源时，才传 `source_ids_json`。
- `novel_refresh_sources` 是异步队列工具；返回后应告诉用户刷新已经开始，而不是假装探测已完成。
- `novel_probe_status` 用来查看探测是否还在跑、队列有多长、某批书源当前健康摘要是什么，不要把它当成下载状态工具。
- 如果导入/列源结果提示 `JS 规则`、`jsLib`、`loginUrl/loginUi` 或类似限制，要明确说明该插件偏向纯 Python 静态书源，这类源成功率会低。
- 如果下载失败一次，先总结失败原因和下一步建议；需要改走候选筛选、阅读更多缓存结果或指定单源时再切分支，不要机械地用同一参数连续重试。

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
- `novel_download_source_book`: 指定 `source_id + book_url + book_name + author` 启动下载，并在预检后再次校验书名和作者。
- `novel_read_search_results`: 按 `search_id` 分页查看缓存搜索结果。
- `novel_download_status`: 查询下载任务进度、状态和结果摘要。

## Boundaries

- 当前对模型已经开放了受控的候选查询、缓存结果读取、`source_id + book_url + book_name + author` 精确下载，以及单源预检能力；不要再说“这些能力未暴露”。
- `novel_download`、`novel_download_cached_result`、`novel_download_book` 等兼容入口仍在代码里，但已经是 hidden，不再给 LLM 直接使用。
- 仍主要保留给人工命令或内部调试的能力包括页面预览、手工 regex 下载、底层任务恢复和其他更自由的调试链路。
- 不要承诺支持需要浏览器渲染、验证码、登录态或复杂 JS 规则的书源。
- `novel_import_sources`、`novel_import_clean_rules` 这类支持“文件路径”输入的工具，在沙箱模式下读取的是沙箱内路径；如果要使用宿主机文件，先用 `astrbot_upload_file`。
- 下载完成后的最终交付优先走 `send_message_to_user`；不要把“发给用户”和“上传到沙箱”混为一谈。

## Response Style

- 下载前：简短说明将使用的书名、作者，以及当前选择的是候选查询、单源检查还是精确下载分支。
- 下载中：只汇报关键进度，不回灌大段 JSON。
- 下载失败：给出失败摘要和最可能的下一步，例如补作者、刷新书源、查看探测状态、先查候选、阅读更多缓存结果或换静态源。
- 下载成功：明确告知任务已完成，并引用工具返回的任务信息或输出文件信息。

## Reference

需要精确查看当前可用工具、参数和人工命令映射时，读取 [references/tool-catalog.md](references/tool-catalog.md)。
