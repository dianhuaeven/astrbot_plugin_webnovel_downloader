# 网文下载器

## 简介

网文下载器是一个面向 AstrBot 的纯 Python 小说下载插件。

它的核心思路不是内置固定站点爬虫，而是复用 Legado/阅读风格书源，完成下面这条链路：

- 导入书源
- 按书名搜索
- 选择候选书目
- 抓取目录与正文
- 输出按章节顺序组装的 TXT

插件内部使用单文件 `job.jsonl` 记录任务状态，因此支持断点续传、失败后继续补抓，以及任务恢复后重新组装。

## 功能

- 支持导入 Legado/阅读风格书源 JSON
- 支持按书名跨书源搜索
- 支持候选源筛选、预检和正文抽样
- 支持后台健康探测，标记书源搜索/预检/下载状态
- 支持正文净化规则导入与自动清洗
- 支持下载完成后自动组装 TXT
- 支持保留下载 journal，便于恢复和排查问题

当前更适合以下书源：

- 静态 HTTP 页面
- 不依赖 WebView
- 不依赖登录态
- 不依赖 JS 规则执行

## 配置方法

插件配置可在 AstrBot WebUI 中填写。

### 基础配置

- `book_sources`
  启动时自动导入的书源列表。每项可填 URL、文件路径，或原始 JSON 文本。

- `clean_rule_sources`
  启动时自动导入的净化规则列表。每项可填 URL、文件路径，或原始规则文本/JSON。

- `auto_probe_on_import`
  导入书源后是否自动加入后台探测队列。默认开启。

- `auto_assemble`
  下载完成后是否自动组装 TXT。默认开启。

- `cleanup_journal_after_assemble`
  组装完成后是否删除 `job.jsonl`。默认关闭，建议保留。

### 抓取与并发

- `max_workers`
  正文下载并发数，默认 `3`。

- `search_max_workers`
  搜索阶段并发数，默认 `6`。

- `probe_max_workers`
  后台探测线程数，默认 `2`。

- `request_timeout`
  下载阶段单次请求超时秒数，必须大于 `0`。

- `search_request_timeout`
  搜索阶段单个书源请求超时秒数，必须大于 `0`。

- `search_time_budget`
  单次跨书源搜索总时间预算，必须大于 `0`。

- `max_retries`
  单章下载最大重试次数。

- `retry_backoff`
  重试退避倍率。

- `use_env_proxy`
  是否使用 AstrBot 进程中的代理环境变量。

- `user_agent`
  抓取时使用的 HTTP `User-Agent`。

- `default_encoding`
  默认网页编码，留空时自动猜测。

### 健康探测

- `probe_keywords`
  后台探测时用于搜书的关键词列表。

### 工具返回控制

- `preview_chars`
  网页预览默认最大字符数。

- `max_preview_fetch_chars`
  单次网页预览允许返回的最大字符数。

- `max_tool_response_chars`
  单次工具摘要最大字符数。

- `max_tool_preview_items`
  列表类工具默认内联预览条数。

- `max_tool_preview_text`
  单段文本预览最大长度。

### 稳定性

- `journal_fsync`
  每次写入后是否强制落盘。更稳，但会更慢。

### 示例

下面是一份常见配置示例：

```json
{
  "book_sources": [
    "https://example.com/source.json"
  ],
  "clean_rule_sources": [],
  "auto_probe_on_import": true,
  "max_workers": 3,
  "search_max_workers": 12,
  "probe_max_workers": 2,
  "request_timeout": 20.0,
  "search_request_timeout": 5.0,
  "search_time_budget": 30.0,
  "max_retries": 3,
  "retry_backoff": 1.6,
  "auto_assemble": true,
  "cleanup_journal_after_assemble": false,
  "use_env_proxy": false,
  "default_encoding": "",
  "user_agent": "",
  "preview_chars": 4000,
  "max_preview_fetch_chars": 1800,
  "max_tool_response_chars": 2800,
  "max_tool_preview_items": 8,
  "max_tool_preview_text": 180,
  "journal_fsync": false
}
```
