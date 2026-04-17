# 网文下载器

面向 AstrBot 的纯 Python 网文下载插件。

它的目标不是内置某个站点的固定爬虫，而是提供一套通用下载内核：导入 Legado/阅读风格书源，按书名搜索，抓取目录与正文，最后输出严格按章节顺序组装的 TXT。

## 这是什么

这个插件主要解决三件事：

- 导入 Legado/阅读风格书源 JSON
- 按书名跨书源搜索
- 下载整本小说并输出 TXT

下载过程使用单文件 `job.jsonl` 作为追加式 journal，所以它天然支持：

- 掉线恢复
- 重启恢复
- 重复运行时只补缺失章节
- 多线程乱序抓取，但最终 TXT 绝对按目录顺序输出

## 适合什么场景

适合：

- 你已经有一批 Legado/阅读风格书源，想在 AstrBot 里搜书并下载
- 你希望下载流程尽量轻，不依赖浏览器渲染
- 你想保留任务状态，避免一次失败就整本重来
- 你想先本地验证书源兼容性，再接回 AstrBot

不适合：

- 依赖 JS 规则的书源
- 依赖登录态、验证码、动态签名的书源
- 必须通过 WebView 或浏览器渲染才能出正文的站点

## 快速开始

第一次上手，建议只走这 3 步：

1. 导入书源
2. 搜索书名
3. 从搜索结果直接发起下载

如果你是在 AstrBot 里手动操作，可以直接用：

```text
novel_import <书源 URL/路径>
novel_auto <书名>
```

然后用下面的命令查看任务状态：

```text
novel_status <job_id>
```

如果你是让 LLM 调函数工具，推荐流程是：

1. `novel_import_sources`
2. `novel_auto_download`
3. `novel_download_status`

如果你已经知道要下载的 `source_id + book_url`，也可以跳过搜索，直接调用：

- `novel_download_book`

## 一个最短可跑通的流程

推荐把第一次使用收敛成下面这条链路：

1. 导入 `shuyuans/json/...` 书源集合
2. 列出书源，确认哪些源被标记为支持搜索或下载
3. 调 `novel_auto_download` 或 `novel_auto`
4. 让插件在 Python 侧完成搜索、候选排序、目录预检和失败回退
5. 轮询 `novel_download_status`

如果你需要更细粒度地人工挑源，仍然可以退回这条传统链路：

1. `novel_search_books`
2. 从结果里拿到 `search_id` 和 `result_index`
3. 调 `novel_download_search_result`
4. 轮询 `novel_download_status`

如果下载成功并启用了自动组装，最终会得到：

- `jobs/<job_id>/job.jsonl`
- `downloads/<book_name>.txt`

## 常见失败原因

第一次接入这类书源时，最常见的问题不是“代码报错”，而是“书源本身超出当前兼容范围”。

如果导入结果里出现这些提示：

- `ruleSearch 含 JS 规则`
- `ruleBookInfo/ruleToc/ruleContent 含 JS 规则`
- `检测到 jsLib`
- `检测到 loginUrl/loginUi`

通常意味着这个源依赖：

- JS 执行
- 登录态
- WebView
- 站点侧动态签名

当前这条纯 Python 路线只能部分支持，甚至完全不支持。最省时间的处理方式通常不是继续硬兼容，而是换一个更静态的书源。

另外还有几类常见情况：

- `rss/json/...` 链接通常不是标准搜书源，往往不适合“搜索书名 -> 下载整本”
- 目录能抓到但正文为空，通常是 `ruleContent` 不兼容或正文页需要动态渲染
- 搜索结果很多但下载失败，常见原因是详情页、目录页、正文页规则并不完整匹配
- 正文里广告很多，通常不是抓取失败，而是还没有配置净化规则

## 本地先跑稳

如果你不想一开始就接 AstrBot，可以先在本地做“导书源 -> 看兼容性 -> 搜书”。

在仓库父目录执行：

```bash
python -m astrbot_plugin_webnovel_downloader.local_smoke \
  --data-dir /tmp/webnovel-smoke \
  --source-json https://www.yckceo.com/yuedu/shuyuans/json/id/1099.json \
  --list-sources
```

继续搜索：

```bash
python -m astrbot_plugin_webnovel_downloader.local_smoke \
  --data-dir /tmp/webnovel-smoke \
  --keyword 诡秘之主 \
  --limit 20
```

这条本地 CLI 的目标是：

- 不依赖 AstrBot 事件系统
- 直接复用 `SourceRegistry + SearchService + RuleEngine`
- 继续沿用“摘要返回 + 本地报告文件”策略
- 先把兼容性和错误规模看清楚，再回到 AstrBot

常用参数：

- `--source-json`: 支持 URL、文件路径、原始 JSON 文本
- `--list-sources`: 导入后立刻输出一页书源摘要
- `--source-ids-json`: 只测试一小批 `source_id`
- `--include-disabled`: 搜索时连禁用书源一起测
- `--data-dir`: 本地运行目录，可重复复用

## 兼容范围

当前支持的目标范围是“Legado/阅读风格书源子集”：

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

如果你最近碰到这两类链接，它们的定位其实不同：

- `https://www.yckceo.com/yuedu/shuyuans/json/id/xxxx.json`
- `https://www.yckceo.com/yuedu/rss/json/id/xxxx.json`

推荐优先使用 `shuyuans/json/...`：

- 它通常是“阅读/Legado 书源集合”
- 更可能带有 `searchUrl`、`ruleSearch`、`ruleBookInfo`、`ruleToc`、`ruleContent`
- 更适合本插件做“导入书源 -> 搜索书名 -> 下载 TXT”

`rss/json/...` 往往是另一类入口：

- 常见是 RSS 源、单链接源、订阅源
- 可能只有 `singleUrl`、`loadWithBaseUrl` 之类字段
- 一般不适合按书名搜索，也不适合自动抓目录后下载整本 TXT

如果你的目标是“搜书并下载”，优先找 `shuyuans/json/...` 这类链接。

## 清洗规则怎么配

当前支持三种主要方式。

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

含义是：

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

它只负责“正文净化”，不负责“搜索”或“抓目录”。

它常用于：

- 删除广告
- 删除站点尾注
- 删除推广文案
- 统一替换乱码或多余空白

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

### 3. 导入净化规则仓库

现在还支持导入“净化规则仓库”作为全局补充层。

常见流程：

1. 用 `novel_import_clean_rules` 导入一份 JSON 或文本净化规则仓库
2. 用 `novel_list_clean_rules` 查看当前已导入的净化仓库
3. 后续所有正文下载都会自动叠加这些规则

支持的净化仓库格式：

- JSON 数组，每项形如 `{"pattern":"广告","replacement":"","isRegex":true,"scope":"某书源"}`
- JSON 对象，顶层含 `rules/items/data/replaceRules`
- 纯文本，每行一条 `正则##替换内容`

其中：

- `scope` 为空表示全局生效
- `scope` 不为空时，会按书源 `source_id/name/source_url/group` 做匹配

## 核心设计

插件的数据目录下，每个任务会生成一个任务目录：

- `jobs/<job_id>/job.jsonl`
- `downloads/<book_name>.txt`

`job.jsonl` 不是简单的章节列表，而是一个追加式 journal。

它会写入这些记录类型：

- `manifest`: 任务元信息与完整目录
- `state`: 流程状态，如 `created`、`downloading`、`assembled`
- `chapter`: 成功下载的章节正文
- `error`: 失败记录

这样做的好处是：

- 崩溃或断电后，只需要重放 `job.jsonl` 就能恢复状态
- 同一章重试成功后，新的 `chapter` 记录会覆盖旧状态
- 组装 TXT 时可以先扫描 journal，建立 `index -> offset` 偏移表，再按顺序 seek 回去读取
- 不需要把整本书一次性读进内存

## 人类命令

除了函数工具，插件也提供了一组更适合人类手动输入的 AstrBot 指令：

- `novel_import <source_json>`
- `novel_import_clean <repo_json> [repo_name]`
- `novel_sources [limit] [offset]`
- `novel_clean_rules [limit] [offset]`
- `novel_search <keyword> [source_ids_json] [limit] [include_disabled]`
- `novel_auto <keyword> [author] [source_ids_json] [search_limit] [attempt_limit] [output_filename] [auto_assemble] [include_disabled]`
- `novel_searches [limit] [offset]`
- `novel_search_results <search_id> [limit] [offset]`
- `novel_download_result <search_id> <result_index> [output_filename] [auto_assemble]`
- `novel_download <source_id> <book_url> [book_name] [output_filename] [auto_assemble]`
- `novel_status [job_id] [limit] [offset]`
- `novel_remove <source_id>`
- `novel_preview <url> [encoding] [max_chars]`
- `novel_jobs`

这套命令与函数工具尽量保持同名同义，区别只是：

- 函数工具给 LLM 用，返回结构化摘要
- 聊天命令给人类用，直接把摘要发回聊天窗口

## 函数工具

插件会向 AstrBot 注册这些函数工具：

- `novel_import_sources`
- `novel_import_clean_rules`
- `novel_list_sources`
- `novel_list_clean_rules`
- `novel_remove_source`
- `novel_search_books`
- `novel_auto_download`
- `novel_list_searches`
- `novel_get_search_results`
- `novel_download_search_result`
- `novel_download_book`
- `novel_fetch_preview`
- `novel_start_download`
- `novel_download_status`
- `novel_assemble_book`
- `novel_list_jobs`

其中有几类内部恢复或管理动作仍然保留在代码内部，不再暴露给 LLM：

- 书源启停
- 书源规则下载任务恢复
- 手工 regex 下载任务恢复

原因是它们更适合由插件内部自动处理，或者由人类命令触发，而不是让 LLM 在上下文里自由组合。

当前最关键的一组能力是：

- 书源管理：`novel_import_sources`、`novel_list_sources`、`novel_remove_source`
- 自动化下载：`novel_auto_download`
- 搜索与下载：`novel_search_books`、`novel_list_searches`、`novel_get_search_results`、`novel_download_search_result`、`novel_download_book`
- 下载内核：`novel_start_download`、`novel_download_status`、`novel_assemble_book`

`novel_auto_download` 会把“搜书 -> 候选排序 -> 目录预检 -> 失败回退 -> 建任务”收敛成一次确定性工具调用，并额外返回：

- `search_id`
- `search_path`
- `selected`
- `attempts`

这样 LLM 不需要再一个个试书源，重复尝试由 Python 侧自动完成。

`novel_search_books` 会把整次搜索结果落到本地搜索缓存中，并返回：

- `search_id`
- `search_path`
- 每条结果的 `result_index`

这让后续流程可以先搜索，再按缓存结果直接下载，而不用重复全网搜索。

`novel_import_clean_rules` 可以导入一份正文净化规则仓库。导入后：

- 书源自己的 `cleanRuleUrl` 继续生效
- 已导入的净化规则仓库也会一起参与正文清洗

## 下载内核参数

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

`novel_download_book` 参数：

- `source_id`: 书源 ID
- `book_url`: 搜索结果返回的书籍详情页地址
- `book_name`: 可选，手动覆盖书名
- `output_filename`: 可选，自定义输出文件名
- `auto_assemble`: 是否自动组装 TXT

## 工具回包策略

LLM 工具不会把大批量 JSON 直接回发到聊天窗口，也不会把大文件内容直接塞进上下文。

默认策略是：

- `novel_import_sources` 返回导入摘要、本地注册表路径和少量书源预览；必要时写本地报告文件
- `novel_list_sources` 返回分页预览；窗口过大时写本地报告文件
- `novel_search_books` 默认返回精简结果；结果太多时写本地报告文件并返回 `report_path`
- `novel_search_books` 会额外把搜索结果保存到本地缓存，后续可通过 `search_id` 分页查看或直接下载
- `novel_import_clean_rules` 会把净化规则仓库保存到本地，后续下载时自动应用
- `novel_list_jobs` 和 `novel_download_status` 在批量模式下只返回摘要；窗口过大时写本地报告文件
- `novel_fetch_preview` 会限制最大预览字符数，避免把整页 HTML 直接塞回聊天平台

这套策略的原则是：

- 聊天窗口只看摘要
- LLM 上下文只接收足够小的结果
- 完整结构化数据保存在插件数据目录
- 能分页的接口尽量分页
- 必须保留全量结果时，写本地报告文件

## 配置项

可在 AstrBot WebUI 中配置：

- `book_sources`
- `clean_rule_sources`
- `max_workers`
- `search_max_workers`
- `request_timeout`
- `search_request_timeout`
- `search_time_budget`
- `use_env_proxy`
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

其中：

- `book_sources`: 启动时自动导入的书源列表；每项可填 URL、文件路径，或原始 JSON 文本；启动后会在后台导入，成功导入过且配置未变化的项会跳过重复导入
- `clean_rule_sources`: 启动时自动导入的净化规则列表；每项可填 URL、文件路径，或原始规则内容；启动后会在后台导入，成功导入过且配置未变化的项会跳过重复导入
- `max_workers`: 正文下载的章节并发数
- `search_max_workers`: 搜书阶段的并发数；建议按机器和网络情况逐步调大，例如 `12` 到 `24`
- `request_timeout`: 正文下载等抓取路径的单次请求超时秒数，必须大于 `0`；填 `0` 或负数时，插件会在启动阶段直接报配置错误
- `search_request_timeout`: 搜书阶段单个书源请求的超时秒数，必须大于 `0`；默认跟随 `request_timeout`，通常可以配得更短，例如 `3` 到 `6`
- `search_time_budget`: 单次跨书源搜索的总时间预算，必须大于 `0`；达到预算后会返回当前已拿到的部分结果，避免搜索工具整体超时；若已经拿到足够多标题精确命中的结果，也会提前收手
- `use_env_proxy`: 是否沿用当前进程的 `http_proxy/https_proxy/no_proxy`；默认关闭，避免被 AstrBot 全局代理设置误伤抓取
- `max_tool_response_chars / max_tool_preview_items / max_tool_preview_text`: 控制函数工具和聊天命令的摘要尺寸
- `max_preview_fetch_chars`: 控制 `novel_fetch_preview` / `novel_preview` 的最大返回体积

## 安装方式

将目录 `astrbot_plugin_webnovel_downloader` 放进 AstrBot 的插件目录后重启即可。

## 适用边界

这个插件刻意不把“站点解析规则”写死。它更像一个通用下载内核：

- 如果你已经能拿到章节 TOC，就可以直接下载
- 如果你已经导入了兼容书源，现在可以做“导入书源 + 搜索书名 + 下载 TXT”
- 如果不同网站的正文结构不同，只需要调整 `content_regex` / `title_regex`
- 如果后续你想改成站点专用版，只需要在此基础上增加“目录抓取器”那一层
