---
name: webnovel-downloader-workflow
version: 1.0.1
description: 使用 AstrBot 网文下载插件处理明确目标的小说下载、Legado/阅读书源导入、书源健康查看、缓存候选下载和安全约束。仅在用户已经明确要下载并确定目标作品时使用搜索下载流程；不要将搜索工具用于推荐、探索或随意查询。Use for explicit webnovel download requests with a confirmed target, source management, or plugin safety review. Never use the search tool for discovery or recommendations.
---

# Webnovel Downloader Workflow

在用户已经明确要下载某部小说并确定目标作品，想通过本插件导入 Legado/阅读书源、查看任务进度，或讨论插件后续开发约束时使用本技能。不要因为用户随口提到小说、询问推荐或想了解作品，就调用搜索工具。

## Current Public LLM Surface

1.0.0 对 LLM 只暴露 `webnovel_*` 工具。不要再引用旧的 `novel_*` LLM 函数作为可用工具。

- `webnovel_search_books`: 为一次已确认目标的下载查找候选书源；这是耗时的下载准备步骤，不是通用搜索或推荐工具。
- `webnovel_download_book`: 根据 `search_id + group_index` 下载一本书；下载源必须来自搜索缓存候选组。
- `webnovel_download_status`: 查询单个下载任务，或列出下载任务。
- `webnovel_import_sources`: 管理员导入 Legado/阅读书源 JSON。
- `webnovel_list_sources`: 查看已导入书源、能力摘要和健康状态。
- `webnovel_refresh_sources`: 管理员将书源加入后台健康探测队列。
- `webnovel_probe_status`: 查看后台探测状态和书源健康摘要。
- `webnovel_import_clean_rules`: 管理员导入正文净化规则仓库。
- `webnovel_list_clean_rules`: 查看已导入净化规则仓库。

## Search Invocation Gate

`webnovel_search_books` 与下载强绑定。搜索会并发访问多个书源，可能耗时很长；不能把它当作低成本、可随意尝试的查询工具。

只有同时满足以下条件时才允许调用：

- 用户在当前请求中明确要求下载、保存或获取完整小说，而不只是询问、讨论或求推荐。
- 已经确认准确书名；不能使用题材、角色、剧情描述或模糊关键词代替书名。
- 同名作品可能混淆时，已经确认作者。用户未提供必要信息时，先追问，不要先搜索。

以下场景禁止调用：

- 推荐小说、寻找“类似某书”的作品、按题材探索或列书单。
- 用户随口提到书名、询问简介、作者、评价、是否好看或是否存在。
- 为了测试工具、检查网络、查看书源健康或“先搜搜看”。书源检查应使用 `webnovel_list_sources` 或 `webnovel_probe_status`。
- 上一次搜索已经返回足够候选时重复搜索。应复用当前 `search_id`，或先让用户解决候选歧义。

搜索完成后必须服务于当前这次下载：只有一个与已确认书名和作者一致的候选组时，直接调用 `webnovel_download_book`；存在不同作品或作者歧义时，只展示必要选项让用户确认，然后使用同一份搜索缓存下载，不要重新搜索。

## Default Download Flow

1. 如果用户提供书源 URL、书源文件路径或书源 JSON，先用 `webnovel_import_sources` 导入。
2. 如果用户提到广告、页脚、正文脏或净化规则，用 `webnovel_import_clean_rules` 或 `webnovel_list_clean_rules`。
3. 如果用户只是想看现有书源或健康摘要，用 `webnovel_list_sources`。
4. 如果刚导入书源、怀疑健康度过期或想重新探测，用 `webnovel_refresh_sources`。
   - 这是后台异步探测，只表示“已经入队”。
   - 需要确认探测进度时再用 `webnovel_probe_status`。
5. 在任何搜索之前，确认用户明确要求下载，并确认准确书名；同名有歧义时先确认作者。
   - 信息不足时先追问，不调用任何搜索工具。
   - 如果用户只是想搜索、推荐或了解作品，说明该工具只用于下载准备，不执行搜索。
6. 对已确认的目标只调用一次 `webnovel_search_books`。
   - 返回的 `search_id` 是后续下载唯一入口。
   - 返回的 `candidate_groups` 是按同名同作者聚合后的书籍组。
7. 搜索结果与已确认目标唯一匹配时，紧接着用 `webnovel_download_book(search_id, group_index)`；有实质歧义时先让用户选择候选组。
   - 不要传外部 `book_url`。
   - 不要自行拼 URL。
   - 不要为了下载同一目标再次搜索；复用当前 `search_id`。
   - 不要恢复旧的“指定任意 URL 下载”流程。
8. 任务创建后等待最终通知，不主动轮询或发送中间进度；只有用户主动询问时才调用 `webnovel_download_status`。
   - 轮询要克制，不要高频重复查询。

## Long Task Notification Discipline

- 搜索、导入、刷新源、预检、下载、装订和后台质量探测都可能是长任务；除非遇到必须用户决策的阻塞问题，不要在执行过程中主动给用户发送中间进度消息。
- 后台任务只应在最终成功或失败时通知一次；不要按百分比、章节批次、轮询状态或内部阶段连续刷屏。
- 群聊场景默认按最克制路径处理：不发过程消息，不要求用户手动轮询，最终结果由插件完成回调或用户主动查询返回。
- 如果用户明确要求查看进度，可以使用 `webnovel_download_status` 或 `webnovel_probe_status` 做一次性查询；不要自行安排高频重复查询。

## Security Red Lines

这些是审查报告沉淀下来的硬约束。后续开发、文档、skill 和工具目录都必须尊重。

- 不要把任意外部 `book_url` 作为普通用户可传参数暴露给 LLM 或普通命令。
- 下载必须来自搜索缓存候选，或者经过同源校验、scheme 校验和内网地址拒绝。
- 默认拒绝 `file://`，并拒绝 loopback、private、link-local、本机名和其他内网地址。
- `novel_download`、`novel_inspect_source_book`、页面预览、手工 regex 下载等自由 URL 能力只应保留为受控管理员诊断路径，不能作为普通下载入口。
- QuickJS 规则执行没有可靠墙钟超时前，不要宣传“安全沙箱”；对不可信 JS 书源默认谨慎或禁用。
- 不要让第三方规则的 `while(true){}` 有机会挂住搜索/下载工作线程；长期方案是子进程硬超时或默认禁用不可信 JS。
- 自动安装 bundled skill 是本插件的预期行为；保留时必须幂等、可配置、可追踪，并记录版本、来源和失败原因。失败不能影响插件主功能。
- 任何支持文件路径或 URL 的管理工具，都要明确权限边界和读取位置；沙箱场景下宿主机文件应先上传到沙箱。

## Engineering Debt To Track

这些不是 LLM 调用流程，但属于后续维护任务里的优先事项。

- 测试应能在仓库目录直接运行；补 `pyproject.toml`、安装入口或 pytest 配置，避免必须从父目录手动加 `PYTHONPATH`。
- 拆分过胖文件：`core/rule_engine.py`、`base.py`、`tests/test_local_smoke.py`。
- JSON 状态的读-改-写需要锁或统一迁移到 SQLite，避免并发工具调用丢更新。
- 减少静默 `except Exception`；失败应记录可诊断原因，不要把真实错误伪装成“无规则/不可用/偶发失败”。
- 书源、净化规则、搜索缓存、下载任务这些共享状态要有一致的并发策略。

## Response Style

- 搜索前：确认用户当前明确要下载，并复述准确书名和作者；信息不全或有歧义时先追问。
- 搜索后：不要把结果当作推荐列表展开；仅在候选有实质歧义时提供最少必要选项，否则直接进入下载。
- 下载中：不主动汇报百分比、章节批次或定时进度；只在用户主动询问时查询一次。
- 下载失败：给出失败摘要和下一步建议，例如补作者、刷新书源、查看探测状态、换静态源或换候选组。
- 下载成功：最终通知一次，明确任务完成并引用逻辑文件名或任务信息，不暴露宿主机绝对路径。
- 讨论开发时：优先指出是否触碰安全红线，再谈实现细节。

## Reference

需要精确查看当前可用工具、参数、旧命令映射和安全边界时，读取 [references/tool-catalog.md](references/tool-catalog.md)。
