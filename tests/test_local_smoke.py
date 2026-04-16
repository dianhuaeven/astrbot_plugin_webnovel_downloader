from __future__ import annotations

import asyncio
import inspect
import importlib
import json
import shutil
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, get_args, get_origin
from urllib.parse import unquote_to_bytes, urlsplit


SUPPORTED_TOOL_TYPES = {str}


def _validate_tool_signature(func):
    annotations = getattr(func, "__annotations__", {})
    for name, annotation in annotations.items():
        if name in ("return", "self"):
            continue
        if name == "event":
            continue
        if not _is_supported_annotation(annotation):
            raise ValueError(
                "LLM 函数工具 {name} 不支持的参数类型：{annotation}".format(
                    name=func.__name__,
                    annotation=_annotation_name(annotation),
                )
            )


def _annotation_name(annotation) -> str:
    if annotation is int:
        return "integer"
    if annotation is bool:
        return "boolean"
    if annotation is str:
        return "string"
    return getattr(annotation, "__name__", str(annotation))


def _is_supported_annotation(annotation) -> bool:
    if isinstance(annotation, str):
        return annotation in {"str", "string"}
    if annotation in SUPPORTED_TOOL_TYPES:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    if origin is list:
        args = get_args(annotation)
        return bool(args) and all(_is_supported_annotation(arg) for arg in args)
    if origin is dict:
        args = get_args(annotation)
        return len(args) == 2 and all(_is_supported_annotation(arg) for arg in args)
    return False


class PluginSmokeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.plugin_dir = Path("/home/dianhua/Code/Python/astrbot_plugin_webnovel_downloader")

        self._install_astrbot_stubs()
        self.module = importlib.import_module("astrbot_plugin_webnovel_downloader.main")
        self.plugin = self.module.JsonlNovelDownloaderPlugin(context=object(), config={})

    def tearDown(self):
        for name in list(sys.modules):
            if name.startswith("astrbot"):
                sys.modules.pop(name, None)
        sys.modules.pop("astrbot_plugin_webnovel_downloader.main", None)
        self.tempdir.cleanup()

    def _install_astrbot_stubs(self):
        astrbot = types.ModuleType("astrbot")
        astrbot_api = types.ModuleType("astrbot.api")
        astrbot_api_event = types.ModuleType("astrbot.api.event")
        astrbot_api_star = types.ModuleType("astrbot.api.star")
        astrbot_core = types.ModuleType("astrbot.core")
        astrbot_core_star = types.ModuleType("astrbot.core.star")
        astrbot_core_star_tools = types.ModuleType("astrbot.core.star.star_tools")

        class DummyStar(object):
            def __init__(self, context):
                self.context = context

        class DummyFilter(object):
            @staticmethod
            def command(_name):
                def decorator(func):
                    return func

                return decorator

            @staticmethod
            def llm_tool(*_args, **_kwargs):
                return llm_tool(*_args, **_kwargs)

        def register(*_args, **_kwargs):
            def decorator(cls):
                return cls

            return decorator

        def llm_tool(name=None):
            def decorator(func):
                _validate_tool_signature(func)
                func.__llm_tool_name__ = name or func.__name__
                return func

            return decorator

        class DummyMessageEventResult(object):
            def __init__(self):
                self.chain = []
                self.text = ""

            def message(self, text):
                self.text = text
                self.chain.append(text)
                return self

        class DummyEvent(object):
            def plain_result(self, text):
                return DummyMessageEventResult().message(text)

        class DummyStarTools(object):
            @staticmethod
            def get_data_dir():
                return str(self.base_dir / "plugin_data")

        astrbot_api.logger = types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        )
        astrbot_api.llm_tool = llm_tool
        astrbot_api_event.AstrMessageEvent = DummyEvent
        astrbot_api_event.filter = DummyFilter
        astrbot_api_star.Context = object
        astrbot_api_star.Star = DummyStar
        astrbot_api_star.register = register
        astrbot_core_star_tools.StarTools = DummyStarTools

        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = astrbot_api
        sys.modules["astrbot.api.event"] = astrbot_api_event
        sys.modules["astrbot.api.star"] = astrbot_api_star
        sys.modules["astrbot.core"] = astrbot_core
        sys.modules["astrbot.core.star"] = astrbot_core_star
        sys.modules["astrbot.core.star.star_tools"] = astrbot_core_star_tools

    def _start_search_server(self):
        records: dict[str, object] = {
            "get_keyword": "",
            "post_keyword": "",
            "post_method": "",
        }

        class Handler(BaseHTTPRequestHandler):
            def _decode_form_keyword(self, text: str, field_name: str, encoding: str) -> str:
                for part in text.split("&"):
                    if "=" not in part:
                        continue
                    key, value = part.split("=", 1)
                    if key != field_name:
                        continue
                    return unquote_to_bytes(value.replace("+", " ")).decode(encoding)
                return ""

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path != "/search-gbk":
                    self.send_response(404)
                    self.end_headers()
                    return
                keyword = self._decode_form_keyword(parsed.query, "key", "gbk")
                records["get_keyword"] = keyword
                payload = {
                    "data": {
                        "items": [
                            {
                                "title": "GET命中",
                                "author": "作者A",
                                "url": "/books/get-hit",
                                "intro": keyword,
                            }
                        ]
                    }
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            def do_POST(self):
                if self.path != "/search-post":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length).decode("ascii")
                keyword = self._decode_form_keyword(body, "searchkey", "gbk")
                records["post_keyword"] = keyword
                records["post_method"] = self.command
                payload = {
                    "data": {
                        "items": [
                            {
                                "title": "POST命中",
                                "author": "作者B",
                                "url": "/books/post-hit",
                                "intro": keyword,
                            }
                        ]
                    }
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)
        return "http://127.0.0.1:{port}".format(port=server.server_address[1]), records

    async def _invoke_tool(self, tool_callable, *args):
        event = sys.modules["astrbot.api.event"].AstrMessageEvent()
        result = tool_callable(event, *args)
        if inspect.isasyncgen(result):
            chunks = []
            async for item in result:
                chunks.append(item)
            self.assertTrue(chunks)
            result = chunks[0]
        else:
            result = await result
        self.assertIsInstance(
            result,
            str,
            "llm_tool 应返回字符串给 LLM，而不是 MessageEventResult/直接发送消息对象",
        )
        return result

    async def test_llm_tools_end_to_end(self):
        chapters_dir = self.base_dir / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "1.html").write_text(
            "<html><head><title>第一章 降生</title></head>"
            "<body><h1>第一章 降生</h1><div id='content'><p>这是第一章。广告</p></div></body></html>",
            encoding="utf-8",
        )
        (chapters_dir / "2.html").write_text(
            "<html><head><title>第二章 练剑</title></head>"
            "<body><h1>第二章 练剑</h1><div id='content'><p>这是第二章。广告尾注</p></div></body></html>",
            encoding="utf-8",
        )
        (self.base_dir / "clean_rules.txt").write_text(
            "尾注##\n",
            encoding="utf-8",
        )
        (self.base_dir / "book.html").write_text(
            "<html><head><title>雪中悍刀行</title></head><body>"
            "<h1>雪中悍刀行</h1>"
            "<div class='author'>烽火戏诸侯</div>"
            "<div id='intro'>测试简介</div>"
            "<div id='toc'>"
            "<a href='{c1}'>第一章 降生</a>"
            "<a href='{c2}'>第二章 练剑</a>"
            "</div>"
            "</body></html>".format(
                c1=(chapters_dir / "1.html").resolve().as_uri(),
                c2=(chapters_dir / "2.html").resolve().as_uri(),
            ),
            encoding="utf-8",
        )

        source_json = json.dumps(
            [
                {
                    "bookSourceName": "测试JSON源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "search.json").resolve().as_uri(),
                    "cleanRuleUrl": (self.base_dir / "clean_rules.txt").resolve().as_uri(),
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "author": "author",
                        "bookUrl": "url",
                        "intro": "intro",
                    },
                    "ruleBookInfo": {
                        "name": "h1&&text",
                        "author": ".author&&text",
                        "intro": "#intro&&text",
                    },
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "title": "h1&&text",
                        "content": "#content&&text##广告##",
                    }
                }
            ],
            ensure_ascii=False,
        )
        (self.base_dir / "search.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "雪中悍刀行",
                                "author": "烽火戏诸侯",
                                "url": (self.base_dir / "book.html").resolve().as_uri(),
                                "intro": "测试简介",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        import_result = json.loads(await self._invoke_tool(self.plugin.novel_import_sources, source_json))
        self.assertEqual(import_result["imported_count"], 1)
        self.assertTrue(Path(import_result["registry_path"]).exists())
        self.assertEqual(import_result["source_count"], 1)

        listed_sources = json.loads(await self._invoke_tool(self.plugin.novel_list_sources))
        self.assertEqual(listed_sources["total_count"], 1)
        self.assertEqual(listed_sources["sources"][0]["name"], "测试JSON源")

        search_result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "雪中"))
        self.assertEqual(search_result["searched_sources"], 1)
        self.assertGreaterEqual(search_result["result_count"], 1)
        self.assertEqual(search_result["results"][0]["title"], "雪中悍刀行")
        self.assertEqual(
            search_result["results"][0]["book_url"],
            (self.base_dir / "book.html").resolve().as_uri(),
        )

        preview = json.loads(
            await self._invoke_tool(
                self.plugin.novel_fetch_preview,
                (chapters_dir / "1.html").resolve().as_uri(),
                "",
                "200",
            )
        )
        self.assertIn("第一章 降生", preview["text_preview"])

        source_id = listed_sources["sources"][0]["source_id"]
        auto_download_text = await self._invoke_tool(
            self.plugin.novel_download_book,
            source_id,
            (self.base_dir / "book.html").resolve().as_uri(),
            "雪中悍刀行",
            "",
            "true",
        )
        self.assertIn("已创建并启动任务", auto_download_text)
        auto_job_id = auto_download_text.splitlines()[0].split(": ", 1)[1]
        await self.plugin._running_tasks[auto_job_id]
        auto_status = await self._invoke_tool(self.plugin.novel_download_status, auto_job_id)
        self.assertIn("状态: assembled", auto_status)
        auto_output_path = self.plugin.manager.output_dir / "雪中悍刀行.txt"
        self.assertTrue(auto_output_path.exists())
        auto_content = auto_output_path.read_text(encoding="utf-8")
        self.assertIn("第一章 降生", auto_content)
        self.assertIn("这是第一章。", auto_content)
        self.assertNotIn("广告", auto_content)
        self.assertNotIn("尾注", auto_content)

        toc_json = json.dumps(
            [
                {
                    "title": "第一章 降生",
                    "url": (chapters_dir / "1.html").resolve().as_uri(),
                },
                {
                    "title": "第二章 练剑",
                    "url": (chapters_dir / "2.html").resolve().as_uri(),
                },
            ],
            ensure_ascii=False,
        )
        start_text = await self._invoke_tool(
            self.plugin.novel_start_download,
            "测试小说",
            toc_json,
            r"<div id='content'>(.*?)</div>",
            r"<h1>(.*?)</h1>",
            "",
            "",
            "",
            "true",
        )
        self.assertIn("已创建并启动任务", start_text)
        job_id = start_text.splitlines()[0].split(": ", 1)[1]

        await self.plugin._running_tasks[job_id]

        status_text = await self._invoke_tool(self.plugin.novel_download_status, job_id)
        self.assertIn("状态: assembled", status_text)

        assembled_text = await self._invoke_tool(self.plugin.novel_assemble_book, job_id, "false")
        self.assertIn("状态: assembled", assembled_text)

        output_path = self.plugin.manager.output_dir / "测试小说.txt"
        self.assertTrue(output_path.exists())
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("第一章 降生", content)
        self.assertIn("这是第二章。", content)

    async def test_import_rss_like_source_marks_unsupported(self):
        rss_like_source = json.dumps(
            [
                {
                    "sourceName": "源仓库(官方纯净)",
                    "sourceUrl": "http://yckceo.vip",
                    "singleUrl": True,
                    "loadWithBaseUrl": True,
                    "enableJs": True,
                    "enabled": True,
                }
            ],
            ensure_ascii=False,
        )

        result = json.loads(await self._invoke_tool(self.plugin.novel_import_sources, rss_like_source))
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["supported_search_count"], 0)
        self.assertEqual(result["supported_download_count"], 0)
        self.assertGreater(result["warning_count"], 0)
        self.assertTrue(result["warnings_preview"])
        listed_sources = json.loads(await self._invoke_tool(self.plugin.novel_list_sources))
        source = listed_sources["sources"][0]
        self.assertEqual(source["name"], "源仓库(官方纯净)")
        self.assertFalse(source["supports_search"])
        self.assertFalse(source["supports_download"])
        self.assertTrue(source["issues"])

    async def test_js_heavy_source_marks_partial_support_and_skips_search(self):
        js_heavy_source = json.dumps(
            [
                {
                    "bookSourceName": "番茄脚本源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": "https://example.com/search?q={{key}}",
                    "jsLib": "function helper() { return 'ok'; }",
                    "loginUrl": "function login() {}",
                    "ruleSearch": {
                        "bookList": "<js>JSON.parse(result)</js>",
                        "name": "$.title",
                        "bookUrl": "$.url",
                    },
                    "ruleBookInfo": {
                        "name": "h1&&text",
                    },
                    "ruleToc": {
                        "chapterList": "@js:getChapters()",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "content": "<js>return result;</js>",
                    },
                }
            ],
            ensure_ascii=False,
        )

        result = json.loads(await self._invoke_tool(self.plugin.novel_import_sources, js_heavy_source))
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["supported_search_count"], 0)
        self.assertEqual(result["supported_download_count"], 0)
        listed_sources = json.loads(await self._invoke_tool(self.plugin.novel_list_sources))
        source = listed_sources["sources"][0]
        self.assertTrue(source["has_js_lib"])
        self.assertTrue(source["has_login_flow"])
        self.assertTrue(source["search_uses_js"])
        self.assertTrue(source["download_uses_js"])
        self.assertFalse(source["supports_search"])
        self.assertFalse(source["supports_download"])
        self.assertTrue(any("ruleSearch 含 JS 规则" in issue for issue in source["issues"]))

        search_result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "雪中"))
        self.assertEqual(search_result["searched_sources"], 0)
        self.assertEqual(len(search_result["skipped_sources"]), 1)
        self.assertIn("ruleSearch 含 JS 规则", search_result["skipped_sources"][0]["reason"])

    async def test_download_book_rejects_js_only_download_source_before_fetch(self):
        partial_source = json.dumps(
            [
                {
                    "bookSourceName": "部分可搜不可下",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": "https://example.com/search?q={{key}}",
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "bookUrl": "url",
                    },
                    "ruleBookInfo": {
                        "name": "h1&&text",
                    },
                    "ruleToc": {
                        "chapterList": "<js>return [];</js>",
                    },
                    "ruleContent": {
                        "content": "div.content&&text",
                    },
                }
            ],
            ensure_ascii=False,
        )

        import_result = json.loads(await self._invoke_tool(self.plugin.novel_import_sources, partial_source))
        listed_sources = json.loads(await self._invoke_tool(self.plugin.novel_list_sources))
        source_id = listed_sources["sources"][0]["source_id"]
        self.assertTrue(listed_sources["sources"][0]["supports_search"])
        self.assertFalse(listed_sources["sources"][0]["supports_download"])

        with self.assertRaisesRegex(ValueError, "不支持 TXT 下载"):
            await self._invoke_tool(
                self.plugin.novel_download_book,
                source_id,
                "https://example.com/book/1",
                "测试书",
                "",
                "true",
            )

    async def test_bulk_import_returns_compact_summary_with_local_registry(self):
        sources = [
            {
                "bookSourceName": "测试源{index}".format(index=index),
                "bookSourceUrl": "https://example.com/{index}".format(index=index),
                "searchUrl": "https://example.com/search?q={{key}}&source={index}".format(index=index),
                "ruleSearch": {
                    "bookList": "data.items",
                    "name": "title",
                    "bookUrl": "url",
                },
                "ruleBookInfo": {"name": "h1&&text"},
                "ruleToc": {
                    "chapterList": "#toc a",
                    "chapterName": "text",
                    "chapterUrl": "@href",
                },
                "ruleContent": {"content": "#content&&text"},
            }
            for index in range(12)
        ]
        result = json.loads(
            await self._invoke_tool(
                self.plugin.novel_import_sources,
                json.dumps(sources, ensure_ascii=False),
            )
        )
        self.assertEqual(result["imported_count"], 12)
        self.assertEqual(result["source_count"], 12)
        self.assertLessEqual(len(result["sources_preview"]), self.plugin.max_tool_preview_items)
        self.assertGreater(result["remaining_source_count"], 0)
        self.assertTrue(Path(result["registry_path"]).exists())
        self.assertNotIn("sources", result)

        second_page = json.loads(await self._invoke_tool(self.plugin.novel_list_sources, "", "4", "8"))
        self.assertEqual(second_page["returned_count"], 4)
        self.assertFalse(second_page["has_more"])

        compact_page = json.loads(await self._invoke_tool(self.plugin.novel_list_sources, "", "12", "0"))
        self.assertEqual(compact_page["returned_count"], 12)
        self.assertLessEqual(len(compact_page["sources"]), self.plugin.max_tool_preview_items)
        self.assertGreater(compact_page["omitted_from_inline_count"], 0)
        self.assertIn("report_path", compact_page)
        self.assertTrue(Path(compact_page["report_path"]).exists())

    async def test_search_large_result_writes_local_report(self):
        items = [
            {
                "title": "测试小说{index}".format(index=index),
                "author": "作者{index}".format(index=index),
                "url": "https://example.com/book/{index}".format(index=index),
                "intro": "简介" * 80,
            }
            for index in range(12)
        ]
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "搜索大结果源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "search-many.json").resolve().as_uri(),
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "author": "author",
                        "bookUrl": "url",
                        "intro": "intro",
                    },
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {"content": "#content&&text"},
                }
            ],
            ensure_ascii=False,
        )
        (self.base_dir / "search-many.json").write_text(
            json.dumps({"data": {"items": items}}, ensure_ascii=False),
            encoding="utf-8",
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "测试", "", "12", "false"))
        self.assertEqual(result["result_count"], 12)
        self.assertLessEqual(len(result["results"]), self.plugin.max_tool_preview_items)
        self.assertIn("report_path", result)
        self.assertTrue(Path(result["report_path"]).exists())

    async def test_search_supports_legado_request_options_for_get_and_post(self):
        base_url, records = self._start_search_server()
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "GBK GET 源",
                    "bookSourceUrl": base_url,
                    "searchUrl": "/search-gbk?key={{key}}&page={{page}},{\"charset\":\"gbk\"}",
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "author": "author",
                        "bookUrl": "url",
                        "intro": "intro",
                    },
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {"content": "#content&&text"},
                },
                {
                    "bookSourceName": "GBK POST 源",
                    "bookSourceUrl": base_url,
                    "searchUrl": "/search-post,{\"method\":\"POST\",\"charset\":\"gbk\",\"body\":\"searchkey={{key}}&searchtype=all\"}",
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "author": "author",
                        "bookUrl": "url",
                        "intro": "intro",
                    },
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {"content": "#content&&text"},
                },
            ],
            ensure_ascii=False,
        )
        await self._invoke_tool(self.plugin.novel_import_sources, source_json)

        result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "诡秘之主", "", "10", "false"))
        self.assertEqual(result["searched_sources"], 2)
        self.assertEqual(result["successful_sources"], 2)
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(records["get_keyword"], "诡秘之主")
        self.assertEqual(records["post_keyword"], "诡秘之主")
        self.assertEqual(records["post_method"], "POST")
        self.assertCountEqual(
            [item["title"] for item in result["results"]],
            ["GET命中", "POST命中"],
        )

    async def test_list_jobs_large_result_writes_local_report(self):
        for index in range(12):
            self.plugin.manager.create_job(
                "测试任务{index}".format(index=index),
                [
                    {
                        "title": "第一章",
                        "url": "file:///tmp/chapter-{index}.html".format(index=index),
                    }
                ],
                self.module.ExtractionRules(content_regex=r"(?s)(.*)"),
                "",
                "",
                "",
            )

        jobs = json.loads(await self._invoke_tool(self.plugin.novel_list_jobs, "12", "0"))
        self.assertEqual(jobs["returned_count"], 12)
        self.assertLessEqual(len(jobs["jobs"]), self.plugin.max_tool_preview_items)
        self.assertGreater(jobs["omitted_from_inline_count"], 0)
        self.assertIn("report_path", jobs)
        self.assertTrue(Path(jobs["report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
