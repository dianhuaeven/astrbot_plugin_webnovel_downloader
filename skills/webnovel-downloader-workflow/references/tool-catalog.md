# Tool Catalog

## Exposed LLM Tools In 1.0.0

| Tool | Main use | Key inputs | Notes |
| --- | --- | --- | --- |
| `webnovel_search_books` | 为已确认目标的当前下载查找并聚合候选书源 | `keyword`, `author`, `limit`, `include_disabled` | 耗时的下载准备步骤；禁止用于推荐、探索或随意查询；返回缓存供紧接着下载 |
| `webnovel_download_book` | 下载缓存候选组中的一本书 | `search_id`, `group_index`, `attempt_limit`, `output_filename`, `auto_assemble`, `skip_source_ids` | 不接受外部 `book_url`；可临时排除多个坏源，组内多源预检后只创建一个正式任务 |
| `webnovel_download_status` | 查询进度或任务列表 | `job_id`, `limit`, `offset` | 未传 `job_id` 时返回任务列表摘要 |
| `webnovel_send_book` | 发送已完成的小说缓存 | `job_id`, `book_name`, `author` | 普通用户可用；只发送插件下载目录内的完成文件，不依赖电脑工具 |
| `webnovel_import_sources` | 导入 Legado/阅读书源 | `source_json` | 管理员工具；支持 URL、文件路径或原始 JSON |
| `webnovel_list_sources` | 查看书源清单和健康摘要 | `enabled_only`, `limit`, `offset` | 适合确认哪些源可参与搜索或下载 |
| `webnovel_refresh_sources` | 刷新书源健康度 | `source_ids_json`, `include_disabled` | 管理员工具；后台异步探测，不等待完成 |
| `webnovel_probe_status` | 查看后台探测进度和健康摘要 | `source_ids_json`, `include_disabled`, `limit`, `offset` | 用于接住 refresh 后的异步探测过程 |
| `webnovel_import_clean_rules` | 导入正文净化规则仓库 | `repo_json`, `repo_name` | 管理员工具；后续下载会自动应用 |
| `webnovel_list_clean_rules` | 查看净化规则仓库 | `limit`, `offset` | 仅做查看 |

## Removed From Public LLM Surface

旧 `novel_*` 工具名不再作为 LLM 工具使用。不要在工作流里调用或推荐它们。

| Old path | Current status | Replacement |
| --- | --- | --- |
| `novel_download` | 不应暴露；自由 URL 下载风险高 | `webnovel_search_books` -> `webnovel_download_book` |
| `novel_inspect_source_book` | 不应暴露给普通 LLM 流程；自由 URL 预检风险高 | 使用缓存候选组和后台探测摘要 |
| `novel_download_source_book` | 不再作为 1.0 LLM 工具 | `webnovel_download_book` |
| `novel_query_candidates` | 不再作为 1.0 LLM 工具 | `webnovel_search_books` |
| `novel_read_search_results` | 不再作为 1.0 LLM 工具 | 由 `webnovel_search_books` 返回聚合摘要；必要时开发新分页工具 |
| `novel_download_cached_result` | 不应暴露 | `webnovel_download_book` |
| `novel_start_download` | 手工 regex 诊断路径，不给 LLM | 无 |
| `novel_fetch_preview` | 管理员/隐藏诊断路径 | 无 |

## Search Invocation Contract

- 只有用户在当前请求中明确要求下载完整小说，并已确认准确书名时，才调用 `webnovel_search_books`。
- 同名作品可能混淆且作者未知时，必须先追问作者；不要用一次昂贵搜索代替澄清。
- 不得用于推荐、题材探索、作品介绍、闲聊、可用性检查、工具测试或“先搜搜看”。
- 每个已确认下载目标原则上只搜索一次。搜索结果唯一匹配时直接下载；存在作品歧义时从现有 `candidate_groups` 让用户选择，不重复搜索。
- 查看书源是否可用应调用 `webnovel_list_sources` 或 `webnovel_probe_status`，不能用小说搜索充当健康检查。
- 已确认下载目标后必须先调用 `webnovel_send_book` 检查缓存；只有返回 `no_cache` 才允许搜索。

## Safe Download Contract

- LLM 下载入口只能使用 `search_id + group_index`。
- `search_id` 必须来自最近一次 `webnovel_search_books` 返回的搜索缓存。
- `skip_source_ids` 支持单个 ID、逗号/中文逗号/换行分隔或 JSON 数组，只影响本轮选择。
- `group_index` 必须来自该搜索结果的 `candidate_groups`。
- 工具内部可以在候选组内尝试多个源，但最终只创建一个正式任务。
- 不允许 LLM 或普通用户传入任意 `book_url` 触发抓取。
- 如果未来恢复单源预检或 URL 诊断工具，必须至少满足：
  - admin only；
  - 禁 `file://`；
  - 拒绝 loopback/private/link-local/本机地址；
  - 只允许 http/https；
  - 同源校验或候选缓存校验；
  - 明确日志记录。

## QuickJS Policy

- 目前 QuickJS 规则执行不能被视为强沙箱。
- 在没有子进程硬超时或等价墙钟超时前，不要默认信任第三方 JS 规则。
- 看到 `jsLib`、`loginUrl/loginUi`、`webView`、复杂 `@js` 时，应提示兼容性和安全边界。
- 搜索线程的 `Future.cancel()` 杀不掉已经卡住的 JS 工作线程，这一点不要在说明中轻描淡写。

## Storage And Bootstrap Policy

- 自动安装 bundled skill 是本插件的预期行为；必须保持幂等、可配置、可追踪，并记录版本、来源和失败原因。失败不能影响插件主功能。
- JSON 注册表、搜索缓存、净化规则仓库等共享状态若继续使用读-改-写文件，必须补锁；长期建议统一 SQLite。
- 静默吞错会隐藏真实故障；新增代码应记录失败来源、源 ID、阶段和可读错误摘要。

## Human Command Notes

人工命令可以保留用于兼容或诊断，但不要把它们等同于 LLM 工具面。

| Human command / path | Notes |
| --- | --- |
| `novel_import` | 导入书源，对应 `webnovel_import_sources` |
| `novel_sources` | 查看书源，对应 `webnovel_list_sources` |
| `novel_refresh` | 刷新探测，对应 `webnovel_refresh_sources` |
| `novel_search` / `novel_auto` | 应只返回聚合候选摘要，不直接下载 |
| `novel_download` | 若保留，必须避免任意 URL SSRF/本地文件读取风险 |
| `novel_preview` | 管理员诊断路径，必须受 URL 安全策略约束 |
| `novel_status` | 查看下载状态，对应 `webnovel_download_status` |

## File Transfer Notes

- 小说文件只能通过 `webnovel_send_book` 发送。它自行校验文件必须来自插件下载目录。
- 不要使用电脑工具、Shell、`astrbot_upload_file` 或 `send_message_to_user` 读取和发送小说缓存。
- `webnovel_send_book` 返回 `sent` 后文件已经发出，只需给用户一句简短确认。
