# 网文下载器

面向 AstrBot 的网文下载插件。插件复用 Legado/阅读风格书源，支持导入书源、搜索小说、聚合同名同作者的多源结果、自动选择可用书源下载，并装订为 TXT 文件。

它不是固定站点爬虫，核心能力来自书源规则。只要书源的搜索、目录和正文规则可被插件解析，就可以在 AstrBot 中完成搜索、下载、续传、净化和任务状态查询。

## 主要功能

- 导入 Legado/阅读书源 JSON。
- 按书名搜索小说，并合并同名同作者的不同来源。
- 下载时在候选书源中预检可用源，只创建一个正式下载任务。
- 支持正文净化规则，清理广告、页脚和杂质文本。
- 使用 `job.jsonl` 保存任务进度，支持失败后继续补抓和重新装订。
- 输出单本 TXT 文件，章节顺序稳定。
- 后台探测书源的搜索、目录预检和正文下载可用性。

## 使用方法

1. 在 AstrBot 中安装并启用插件。
2. 管理员导入 Legado/阅读书源。
3. 使用搜索工具查找小说，查看返回的 `search_id` 和 `candidate_groups`。
4. 选择目标书籍的 `group_index` 发起下载。
5. 使用状态工具查看下载进度和 TXT 输出路径。

下载数据保存在 AstrBot 的插件数据目录中，包括书源注册表、搜索缓存、下载任务 journal、正文净化规则和最终 TXT。

## LLM 函数

| 函数 | 权限 | 用途 |
| --- | --- | --- |
| `webnovel_search_books` | 普通用户 | 搜索小说，并按同名同作者聚合不同书源。 |
| `webnovel_download_book` | 普通用户 | 根据 `search_id` 和 `group_index` 下载一本书。 |
| `webnovel_download_status` | 普通用户 | 查询单个下载任务，或列出下载任务。 |
| `webnovel_import_sources` | 管理员 | 导入 Legado/阅读书源 JSON。 |
| `webnovel_list_sources` | 普通用户 | 查看已导入书源、能力摘要和健康状态。 |
| `webnovel_refresh_sources` | 管理员 | 将书源加入后台健康探测队列。 |
| `webnovel_probe_status` | 普通用户 | 查看后台探测状态和书源健康摘要。 |
| `webnovel_import_clean_rules` | 管理员 | 导入正文净化规则仓库。 |
| `webnovel_list_clean_rules` | 普通用户 | 查看已导入的净化规则仓库。 |

典型调用顺序：

```text
webnovel_import_sources -> webnovel_search_books -> webnovel_download_book -> webnovel_download_status
```

## 适用书源

更适合静态 HTTP 页面、无需登录、无需 WebView 的书源。需要复杂浏览器环境、强登录态、Cookie 校验或重度 JS 渲染的书源可能只能部分可用。

## 注意事项

- 书源来自第三方时，请先确认来源可信。
- 下载质量取决于书源规则和正文净化规则。
- 导入书源、刷新探测和导入净化规则会访问外部 URL 或修改本地插件数据，建议只给管理员使用。
- 如果下载失败，优先查看书源健康状态和任务最近错误，再决定是否换源。
