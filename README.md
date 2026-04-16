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
- `novel_download_book`
- `novel_resume_book_download`
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
- 书源下载：
  - `novel_search_books`
  - `novel_download_book`
  - `novel_resume_book_download`
- 下载内核：
  - `novel_start_download`
  - `novel_resume_download`
  - `novel_download_status`
  - `novel_assemble_book`

`novel_search_books` 会在已导入且启用的书源中按书名搜索，返回统一结果结构；`novel_download_book` 会基于 `source_id + book_url` 自动抓取详情页、目录页和章节正文，并生成 TXT。

这些工具在 AstrBot 中按 `llm_tool` 语义实现：

- 工具给 LLM 的返回值使用 `return str`
- 普通聊天命令才使用 `yield event.plain_result(...)`
- 不会在 `llm_tool` 里把 `MessageEventResult` 当作工具结果返回

## 工具回包策略

LLM 工具不会再把大批量 JSON 结果直接回发到聊天窗口，也不会把无意义的大文件内容直接塞进 LLM 上下文。

现在的默认策略是：

- `novel_import_sources` 只返回导入摘要、本地注册表路径和少量书源预览；必要时写本地报告文件
- `novel_list_sources` 只返回分页预览；请求窗口过大时，把完整窗口写到本地报告文件
- `novel_search_books` 默认返回精简结果；如果结果太多，会把完整结果写到本地报告文件，再返回 `report_path`
- `novel_list_jobs` 和 `novel_download_status` 在批量模式下只返回任务摘要；请求窗口过大时写本地报告文件
- `novel_fetch_preview` 会限制最大预览字符数，避免把整页 HTML 直接塞回聊天平台

这套设计的原则是：

- 聊天窗口只看摘要
- LLM 上下文只接收足够小的结果
- 完整结构化数据保存在插件数据目录
- 能分页的接口尽量分页
- 必须保留全量结果时，写本地报告文件

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

## 书源最小示例

下面是一份当前可用的最小书源示例：

```json
[
  {
    "bookSourceName": "示例书源",
    "bookSourceUrl": "https://example.com",
    "searchUrl": "https://example.com/search?q={{key}}",
    "ruleSearch": {
      "bookList": "data.items",
      "name": "title",
      "author": "author",
      "bookUrl": "url",
      "intro": "intro"
    },
    "ruleBookInfo": {
      "name": "h1&&text",
      "author": ".author&&text",
      "intro": "#intro&&text"
    },
    "ruleToc": {
      "chapterList": "#toc a",
      "chapterName": "text",
      "chapterUrl": "@href"
    },
    "ruleContent": {
      "title": "h1&&text",
      "content": "#content&&text##广告##"
    }
  }
]
```

## 书源链接怎么选

你最近碰到的两类链接，含义其实不一样：

- `https://www.yckceo.com/yuedu/shuyuans/json/id/xxxx.json`
- `https://www.yckceo.com/yuedu/rss/json/id/xxxx.json`

推荐优先使用 `shuyuans/json/...`：

- 它通常是“阅读/Legado 书源集合”
- 更可能带有 `searchUrl`、`ruleSearch`、`ruleBookInfo`、`ruleToc`、`ruleContent`
- 适合本插件做“导入书源 -> 搜索书名 -> 下载 TXT”

`rss/json/...` 往往是另一类入口：

- 常见是 RSS 源、单链接源、订阅源
- 可能只有 `singleUrl`、`loadWithBaseUrl` 之类字段
- 一般不适合按书名搜索，也不适合自动抓目录后下载整本 TXT

所以，如果你要做“搜书并下载”，优先找 `shuyuans/json/...` 这类链接。

## 清洗规则怎么配

当前支持两种清洗方式：

### 1. 内联净化

直接写在规则字符串后面，格式是：

```text
基础规则##正则##替换内容
```

例如：

```json
{
  "ruleContent": {
    "content": "#content&&text##请收藏本站.*####广告##"
  }
}
```

这表示：

- 先按 `#content&&text` 提取正文
- 再删除 `请收藏本站...`
- 再删除 `广告`

### 2. 远程清洗链接

如果你希望把清洗规则放到远程文件里，可以在书源顶层放这些字段之一：

- `cleanRuleUrl`
- `ruleCleanUrl`
- `defaultRuleUrl`
- `cleanUrl`

插件会尝试下载这份规则，并把它应用到正文上。

它的作用就是“正文净化”：

- 删除广告
- 删除站点尾注
- 删除推广文案
- 统一替换乱码或多余空白

也就是说，`cleanRuleUrl` 不负责“搜索”或“抓目录”，它只负责把已经抓到的正文再清洗一遍。

当前支持的远程规则格式：

- JSON 数组

```json
[
  {"regex": "请收藏本站.*", "replacement": ""},
  {"regex": "广告", "replacement": ""}
]
```

- JSON 对象

```json
{
  "rules": [
    {"pattern": "请收藏本站.*", "replace": ""},
    {"pattern": "广告", "replace": ""}
  ]
}
```

- 纯文本，每行一条

```text
请收藏本站.*##
广告##
```

如果同一条书源里既写了内联净化，又配置了 `cleanRuleUrl`，插件会先加载远程清洗规则，再对正文做本地净化整理。

## 现在怎么下 TXT

推荐流程：

1. 用 `novel_import_sources` 导入书源 JSON
2. 用 `novel_search_books` 搜索书名
3. 从搜索结果拿到 `source_id` 和 `book_url`
4. 调用 `novel_download_book`
5. 用 `novel_download_status` 查看状态

一个最常见的实战流程就是：

1. 导入 `shuyuans/json/...` 书源集合
2. 调 `novel_list_sources` 看哪些源被标记为支持搜索/下载
3. 用 `novel_search_books` 搜书名
4. 从结果里拿 `source_id` 和 `book_url`
5. 调 `novel_download_book`
6. 用 `novel_download_status` 看是否已经 `assembled`

如果某个源在导入结果里带有这些提示：

- `ruleSearch 含 JS 规则`
- `ruleBookInfo/ruleToc/ruleContent 含 JS 规则`
- `检测到 jsLib`
- `检测到 loginUrl/loginUi`

那就说明它属于“阅读规则里依赖 JS/登录/WebView 的源”，当前这条纯 Python 路线只能部分支持甚至完全不支持，建议换一个更静态的源。

`novel_download_book` 参数：

- `source_id`: 书源 ID
- `book_url`: 搜索结果返回的书籍详情页地址
- `book_name`: 可选，手动覆盖书名
- `output_filename`: 可选，自定义输出文件名
- `auto_assemble`: 是否自动组装 TXT

## 路线 A 兼容范围

当前书源支持的目标范围是“Legado/阅读风格书源子集”：

- 支持静态 HTTP 书源
- 支持 HTML / JSON 响应判别
- 支持常见 `ruleSearch / ruleBookInfo / ruleToc / ruleContent`
- 支持基础正则净化 `##regex##replacement`
- 支持远程清洗链接
- 支持 JSONPath 与 CSS/XPath 风格解析

当前暂不支持：

- JS 规则
- `jsLib` / `<js>` / `@js:` 这类脚本规则执行
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
- `max_tool_response_chars`
- `max_tool_preview_items`
- `max_tool_preview_text`
- `max_preview_fetch_chars`

## 安装方式

将目录 `astrbot_plugin_webnovel_downloader` 放进 AstrBot 的插件目录后重启即可。

## 适用边界

这个插件刻意不把“站点解析规则”写死。它更像一个通用下载内核：

- 如果你已经能拿到章节 TOC，就可以直接下载
- 如果你已经导入了兼容书源，现在已经可以先做“导入书源 + 搜索书名”
- 如果你已经导入了兼容书源，现在已经可以做“导入书源 + 搜索书名 + 下载 TXT”
- 如果不同网站的正文结构不同，只需要调整 `content_regex` / `title_regex`
- 如果后续你想改成站点专用版，只需要在此基础上增加“目录抓取器”那一层
