# 网文下载器

网文下载器是一个面向 AstrBot 的轻量级小说下载插件，核心目标是：

- 纯 Python 插件逻辑，配合轻量解析依赖执行书源规则
- 支持导入 Legado/阅读风格书源 JSON
- 支持按书名跨书源搜索
- 下载阶段只维护一个追加写的 `job.jsonl`
- 多线程乱序下载，但最终 TXT 严格按目录顺序输出
- 支持掉线恢复、重启恢复、重复运行只补缺失章节
- 组装时不把整本书一次性读进内存

## 核心设计

插件的数据目录下，每个任务会生成一个任务目录：

- `jobs/<job_id>/job.jsonl`
- `downloads/<book_name>.txt`

`job.jsonl` 不是单纯只存章节，而是一个**追加式 journal**：

- `manifest`：任务元信息与完整目录
- `state`：流程状态，如 `created`、`downloading`、`assembled`
- `chapter`：成功下载的章节正文
- `error`：失败记录

这样带来的好处：

- 断电或崩溃后，只需要重放 `job.jsonl` 就能恢复任务状态
- 同一章重试成功后，新的 `chapter` 记录会覆盖旧状态，天然支持“最后一次成功写入为准”
- 组装 TXT 时先扫描一遍 journal，建立 `index -> offset` 偏移表，再按顺序 seek 回去读取，不需要把整本书全部载入内存

## 函数工具

插件会向 AstrBot 注册这些函数工具：

- `novel_import_sources`
- `novel_list_sources`
- `novel_enable_source`
- `novel_remove_source`
- `novel_search_books`
- `novel_fetch_preview`
- `novel_start_download`
- `novel_resume_download`
- `novel_download_status`
- `novel_assemble_book`
- `novel_list_jobs`

当前阶段最关键的是两类工具：

- 书源管理：
  - `novel_import_sources`
  - `novel_list_sources`
  - `novel_enable_source`
  - `novel_remove_source`
- 下载内核：
  - `novel_start_download`
  - `novel_resume_download`
  - `novel_download_status`
  - `novel_assemble_book`

`novel_search_books` 会在已导入且启用的书源中按书名搜索，返回统一结果结构；`novel_start_download` 仍然接受显式 `toc_json`，后续会把“搜索结果 -> 自动抓目录 -> 下载 TXT/EPUB”完整串起来。

`novel_start_download` 参数说明如下：

- `book_name`: 书名
- `toc_json`: 章节目录 JSON 字符串，形如 `[{"title":"第1章","url":"https://..."}, ...]`
- `content_regex`: 正文提取正则，优先使用第一个捕获组
- `title_regex`: 可选，章节标题提取正则
- `source_url`: 可选，目录页地址
- `output_filename`: 可选，自定义 TXT 文件名
- `encoding`: 可选，强制编码
- `auto_assemble`: 下载完成后是否自动组装

建议先用 `novel_fetch_preview` 抓一页预览，再根据 HTML 结构写 `content_regex` 和 `title_regex`。

## 路线 A 兼容范围

当前书源支持的目标范围是“Legado/阅读风格书源子集”：

- 支持静态 HTTP 书源
- 支持 HTML / JSON 响应判别
- 支持常见 `ruleSearch`
- 支持基础正则净化 `##regex##replacement`
- 支持 JSONPath 与 CSS/XPath 风格解析

当前暂不支持：

- JS 规则
- WebView / 浏览器渲染
- 验证码
- 登录态和动态签名

## 配置项

可在 AstrBot WebUI 中配置：

- `max_workers`
- `request_timeout`
- `max_retries`
- `retry_backoff`
- `journal_fsync`
- `auto_assemble`
- `cleanup_journal_after_assemble`
- `default_encoding`
- `preview_chars`
- `user_agent`

## 安装方式

将目录 `astrbot_plugin_webnovel_downloader` 放进 AstrBot 的插件目录后重启即可。

## 适用边界

这个插件刻意不把“站点解析规则”写死。它更像一个通用下载内核：

- 如果你已经能拿到章节 TOC，就可以直接下载
- 如果你已经导入了兼容书源，现在已经可以先做“导入书源 + 搜索书名”
- 如果不同网站的正文结构不同，只需要调整 `content_regex` / `title_regex`
- 如果后续你想改成站点专用版，只需要在此基础上增加“目录抓取器”那一层
