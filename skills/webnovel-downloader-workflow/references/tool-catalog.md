# Tool Catalog

## Exposed LLM Tools

| Tool | Main use | Key inputs | Notes |
| --- | --- | --- | --- |
| `novel_import_sources` | 导入 Legado/阅读书源 | `source_json` | 支持 URL、文件路径或原始 JSON；沙箱模式下若要读取宿主机文件，先用 `astrbot_upload_file` 上传到 `/workspace` |
| `novel_import_clean_rules` | 导入正文净化规则仓库 | `repo_json`, `repo_name` | 后续下载会自动应用；沙箱模式下若要读取宿主机文件，先用 `astrbot_upload_file` 上传到 `/workspace` |
| `novel_list_sources` | 查看书源清单 | `enabled_only`, `limit`, `offset` | 适合确认哪些源可参与搜索或下载 |
| `novel_get_source_detail` | 查看单个书源详情 | `source_id` | 返回健康状态、编译后的 profile 和关键规则摘要 |
| `novel_refresh_sources` | 刷新书源健康度 | `source_ids_json`, `include_disabled` | 后台异步探测，不等待完成 |
| `novel_probe_status` | 查看后台探测进度和健康摘要 | `source_ids_json`, `include_disabled`, `limit`, `offset` | 用于接住 refresh 后的异步探测过程 |
| `novel_list_clean_rules` | 查看净化规则仓库 | `limit`, `offset` | 仅做查看 |
| `novel_remove_source` | 删除书源 | `source_id` | 适合移除失效或重复书源 |
| `novel_query_candidates` | 只查候选源和排序结果 | `keyword`, `author`, `source_ids_json`, `limit`, `offset`, `include_disabled` | 返回 `search_id`，便于后续分页或续下 |
| `novel_inspect_source_book` | 只读检查单个候选源 | `source_id`, `book_url`, `book_name` | 不创建任务，适合做目录预检和正文抽样 |
| `novel_download_source_book` | 指定源精确下载 | `source_id`, `book_url`, `book_name`, `author`, `output_filename`, `auto_assemble` | 要求 `book_name + author` 都与候选结果精确一致 |
| `novel_read_search_results` | 分页读取缓存搜索结果 | `search_id`, `limit`, `offset` | 避免重复发起同一次搜索 |
| `novel_download_status` | 查询进度 | `job_id`, `limit`, `offset` | 未传 `job_id` 时返回任务列表摘要 |

## Manual Command Mapping

这些命令更适合人类手动输入，或对应插件内部/隐藏调试路径。它们和 LLM 工具有不少共享缓存或受控映射，但不等于“这类能力没有暴露给模型”。

| Human command / path | LLM counterpart | Notes |
| --- | --- | --- |
| `novel_import` | `novel_import_sources` | 都用于导入 Legado/阅读书源 |
| `novel_import_clean` | `novel_import_clean_rules` | 都用于导入正文净化规则 |
| `novel_sources` | `novel_list_sources` / `novel_get_source_detail` | LLM 侧拆成列表和单源详情两个入口 |
| `novel_refresh` | `novel_refresh_sources` + `novel_probe_status` | 刷新是异步入队；状态查看单独走 probe 工具 |
| `novel_clean_rules` | `novel_list_clean_rules` | 查看已导入净化规则仓库 |
| `novel_auto` | `novel_query_candidates` | 兼容命令现在也只返回候选摘要，不再直接启动下载 |
| `novel_search` | `novel_query_candidates` / `novel_read_search_results` | LLM 先拿候选摘要和 `search_id`，需要查看更多原始结果时再读缓存 |
| `novel_search_results` | `novel_read_search_results` | 都按 `search_id` 分页读取缓存搜索结果 |
| `novel_download_result` | none | 该命令已停用，提示先查候选再指定源下载 |
| `novel_download <source_id> <book_url>` | `novel_download_source_book` | LLM 侧需要补全 `book_name + author` 后才能精确下载 |
| `novel_status` | `novel_download_status` | 查看任务状态和结果摘要 |
| `novel_remove` | `novel_remove_source` | 删除已导入书源 |
| `novel_preview` | none | 仍主要用于人工诊断页面结构 |
| `novel_jobs` | none | 仍主要用于低层任务调试或恢复 |

## Important Limits

- `astrbot_upload_file` 用于把宿主机文件上传到沙箱 `/workspace`，给后续工具或代码读取；它不是“把文件发给用户”的工具。
- `send_message_to_user` 用于把文本、图片、语音、视频或文件直接发送给当前用户；当插件已经产出宿主机上的下载文件时，优先用它交付结果。
- 当前对 LLM 的默认下载流程是 `novel_query_candidates -> novel_download_source_book`，不要再按旧文档直接调用 `novel_download`。
- 当书名歧义明显、需要人工挑源、要确认探测状态，或已经有 `search_id` 需要翻页/查看更多原始结果时，再切到 `novel_query_candidates`、`novel_probe_status`、`novel_read_search_results`、`novel_download_source_book` 这些分支工具。
- `novel_refresh_sources` 只是把书源加入后台健康探测队列；想确认进度时要再调用 `novel_probe_status`。
- `novel_inspect_source_book` 是只读预检工具，不会创建下载任务。
- `novel_download_source_book` 会在预检后再次校验 `book_name + author`，不一致就拒绝创建任务。
- `novel_download`、`novel_download_cached_result` 等兼容入口仍在代码里，但不再对 LLM 暴露。
- 如果书源提示 JS、登录或动态渲染限制，应尽早向用户说明兼容边界。
- 当工具参数支持“文件路径”时，沙箱模式读取的是沙箱内 `/workspace` 相对路径；宿主机文件需要先通过 `astrbot_upload_file` 上传进去。
- 当插件返回 `output_path` 一类宿主机文件路径且用户想直接拿到文件时，优先把这个路径交给 `send_message_to_user` 的 `file` 消息，而不是先上传回沙箱。
