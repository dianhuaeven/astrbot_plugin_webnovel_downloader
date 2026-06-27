---
name: webnovel-downloader-workflow
version: 1.0.0
description: 使用 AstrBot 网文下载插件处理 Legado/阅读书源导入、书源健康查看、小说搜索聚合、缓存候选下载、下载状态查询和 1.0 发布前安全约束。Use when users want to download web novels through the plugin, manage Legado sources, use the current webnovel_* LLM tools, or review safety constraints for future changes.
---

# Webnovel Downloader Workflow

在用户想通过本插件导入 Legado/阅读书源、搜索并下载小说、查看任务进度，或讨论插件后续开发约束时使用本技能。

## Current Public LLM Surface

1.0.0 对 LLM 只暴露 `webnovel_*` 工具。不要再引用旧的 `novel_*` LLM 函数作为可用工具。

- `webnovel_search_books`: 搜索小说，并按同名同作者聚合不同书源。
- `webnovel_download_book`: 根据 `search_id + group_index` 下载一本书；下载源必须来自搜索缓存候选组。
- `webnovel_download_status`: 查询单个下载任务，或列出下载任务。
- `webnovel_import_sources`: 管理员导入 Legado/阅读书源 JSON。
- `webnovel_list_sources`: 查看已导入书源、能力摘要和健康状态。
- `webnovel_refresh_sources`: 管理员将书源加入后台健康探测队列。
- `webnovel_probe_status`: 查看后台探测状态和书源健康摘要。
- `webnovel_import_clean_rules`: 管理员导入正文净化规则仓库。
- `webnovel_list_clean_rules`: 查看已导入净化规则仓库。

## Default Download Flow

1. 如果用户提供书源 URL、书源文件路径或书源 JSON，先用 `webnovel_import_sources` 导入。
2. 如果用户提到广告、页脚、正文脏或净化规则，用 `webnovel_import_clean_rules` 或 `webnovel_list_clean_rules`。
3. 如果用户只是想看现有书源或健康摘要，用 `webnovel_list_sources`。
4. 如果刚导入书源、怀疑健康度过期或想重新探测，用 `webnovel_refresh_sources`。
   - 这是后台异步探测，只表示“已经入队”。
   - 需要确认探测进度时再用 `webnovel_probe_status`。
5. 下载前先走 `webnovel_search_books`。
   - 返回的 `search_id` 是后续下载唯一入口。
   - 返回的 `candidate_groups` 是按同名同作者聚合后的书籍组。
6. 用户确认目标书籍后，用 `webnovel_download_book(search_id, group_index)`。
   - 不要传外部 `book_url`。
   - 不要自行拼 URL。
   - 不要恢复旧的“指定任意 URL 下载”流程。
7. 任务创建后，用 `webnovel_download_status` 汇报进度和输出路径。
   - 轮询要克制，不要高频重复查询。

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

- 下载前：说明使用的书名、作者和候选组，确认来自 `webnovel_search_books` 的缓存结果。
- 下载中：只汇报关键进度，不回灌大段 JSON。
- 下载失败：给出失败摘要和下一步建议，例如补作者、刷新书源、查看探测状态、换静态源或换候选组。
- 下载成功：明确告知任务完成，并引用输出文件路径或任务信息。
- 讨论开发时：优先指出是否触碰安全红线，再谈实现细节。

## Reference

需要精确查看当前可用工具、参数、旧命令映射和安全边界时，读取 [references/tool-catalog.md](references/tool-catalog.md)。
