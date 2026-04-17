from __future__ import annotations

import asyncio
import inspect
import importlib
import json
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, get_args, get_origin
from urllib.parse import unquote_to_bytes, urlsplit
from urllib.request import Request


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
            name = "astrbot_plugin_webnovel_downloader"

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
            def get_data_dir(plugin_name=None):
                if not plugin_name:
                    raise ValueError("无法获取插件名称")
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

    async def _invoke_command(self, command_callable, *args):
        event = sys.modules["astrbot.api.event"].AstrMessageEvent()
        result = command_callable(event, *args)
        self.assertTrue(inspect.isasyncgen(result))
        chunks = []
        async for item in result:
            chunks.append(item)
        self.assertTrue(chunks)
        command_result = chunks[0]
        self.assertTrue(hasattr(command_result, "text"))
        return str(command_result.text or "")

    def test_llm_tool_surface_excludes_internal_admin_and_resume_helpers(self):
        tool_names = set()
        for attr_name in dir(self.plugin):
            attr = getattr(self.plugin, attr_name)
            tool_name = getattr(attr, "__llm_tool_name__", "")
            if tool_name:
                tool_names.add(tool_name)

        self.assertNotIn("novel_enable_source", tool_names)
        self.assertNotIn("novel_resume_book_download", tool_names)
        self.assertNotIn("novel_resume_download", tool_names)
        self.assertIn("novel_remove_source", tool_names)
        self.assertIn("novel_download_status", tool_names)

    def test_plugin_init_uses_explicit_plugin_name_for_data_dir(self):
        expected = self.base_dir / "plugin_data"
        self.assertEqual(self.plugin.plugin_data_dir, expected)
        self.assertTrue(expected.exists())

    def test_plugin_init_rejects_non_positive_request_timeout(self):
        with self.assertRaisesRegex(ValueError, "request_timeout.*必须大于 0"):
            self.module.JsonlNovelDownloaderPlugin(
                context=object(),
                config={"request_timeout": 0},
            )

    def test_open_url_ignores_env_proxy_by_default(self):
        http_utils = importlib.import_module("astrbot_plugin_webnovel_downloader.http_utils")
        called: dict[str, object] = {}

        class FakeResponse(object):
            status_code = 200
            reason_phrase = "OK"
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            content = b"opened-without-env-proxy"
            url = "https://example.com/final"

        class FakeClient(object):
            def __init__(self, **kwargs):
                called["client_kwargs"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def request(self, **kwargs):
                called["request_kwargs"] = kwargs
                return FakeResponse()

        original_httpx = http_utils.httpx
        http_utils.httpx = types.SimpleNamespace(Client=FakeClient)
        try:
            result = http_utils.open_url(
                Request("https://example.com/test", headers={"User-Agent": "UA"}),
                12.0,
                use_env_proxy=False,
            )
        finally:
            http_utils.httpx = original_httpx

        self.assertEqual(result.read(), b"opened-without-env-proxy")
        self.assertEqual(result.headers.get_content_charset(), "utf-8")
        self.assertEqual(result.url, "https://example.com/final")
        self.assertEqual(called["client_kwargs"]["timeout"], 12.0)
        self.assertEqual(called["request_kwargs"]["method"], "GET")
        self.assertFalse(called["client_kwargs"]["trust_env"])

    def test_open_url_can_use_env_proxy_when_enabled(self):
        http_utils = importlib.import_module("astrbot_plugin_webnovel_downloader.http_utils")
        called: dict[str, object] = {}

        class FakeResponse(object):
            status_code = 200
            reason_phrase = "OK"
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            content = b"opened-with-env-proxy"
            url = "https://example.com/proxied"

        class FakeClient(object):
            def __init__(self, **kwargs):
                called["client_kwargs"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def request(self, **kwargs):
                called["request_kwargs"] = kwargs
                return FakeResponse()

        original_httpx = http_utils.httpx
        http_utils.httpx = types.SimpleNamespace(Client=FakeClient)
        try:
            result = http_utils.open_url(
                Request("https://example.com/test", headers={"User-Agent": "UA"}),
                8.5,
                use_env_proxy=True,
            )
        finally:
            http_utils.httpx = original_httpx

        self.assertEqual(result.read(), b"opened-with-env-proxy")
        self.assertEqual(result.url, "https://example.com/proxied")
        self.assertEqual(called["client_kwargs"]["timeout"], 8.5)
        self.assertTrue(called["client_kwargs"]["trust_env"])

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
        self.assertTrue(search_result["search_id"])
        self.assertTrue(Path(search_result["search_path"]).exists())
        self.assertEqual(search_result["results"][0]["title"], "雪中悍刀行")
        self.assertEqual(search_result["results"][0]["result_index"], 0)
        self.assertEqual(
            search_result["results"][0]["book_url"],
            (self.base_dir / "book.html").resolve().as_uri(),
        )
        cached_results = json.loads(
            await self._invoke_tool(
                self.plugin.novel_get_search_results,
                search_result["search_id"],
                "1",
                "0",
            )
        )
        self.assertEqual(cached_results["total_result_count"], 1)
        self.assertEqual(cached_results["results"][0]["result_index"], 0)

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

    async def test_human_commands_smoke(self):
        (self.base_dir / "search-command.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "命令测试书",
                                "author": "命令作者",
                                "url": (self.base_dir / "cmd-book.html").resolve().as_uri(),
                                "intro": "命令简介",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.base_dir / "cmd-book.html").write_text(
            "<html><body><h1>命令测试书</h1><div class='author'>命令作者</div>"
            "<div id='toc'><a href='{c1}'>第一章</a></div></body></html>".format(
                c1=(self.base_dir / "cmd-chapter-1.html").resolve().as_uri()
            ),
            encoding="utf-8",
        )
        (self.base_dir / "cmd-chapter-1.html").write_text(
            "<html><body><h1>第一章</h1><div id='content'><p>命令正文。</p></div></body></html>",
            encoding="utf-8",
        )
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "命令测试源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "search-command.json").resolve().as_uri(),
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
                    },
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "title": "h1&&text",
                        "content": "#content@p@html",
                    },
                }
            ],
            ensure_ascii=False,
        )

        import_text = await self._invoke_command(self.plugin.novel_import_command, source_json)
        self.assertIn("imported_count", import_text)

        sources_text = await self._invoke_command(self.plugin.novel_sources_command)
        self.assertIn("命令测试源", sources_text)

        search_text = await self._invoke_command(self.plugin.novel_search_command, "命令测试书")
        self.assertIn("search_id", search_text)
        payload = json.loads(search_text)

        status_text = await self._invoke_command(self.plugin.novel_status_command)
        self.assertIn("当前没有任何下载任务", status_text)

        remove_text = await self._invoke_command(
            self.plugin.novel_remove_command,
            payload["results"][0]["source_id"],
        )
        self.assertIn("removed", remove_text)

    def test_plugin_bootstraps_sources_and_clean_rules_from_config(self):
        source_path = self.base_dir / "bootstrap-source.json"
        clean_path = self.base_dir / "bootstrap-clean.json"
        source_path.write_text(
            json.dumps(
                [
                    {
                        "bookSourceName": "配置书源",
                        "bookSourceUrl": "https://example.com",
                        "searchUrl": "https://example.com/search?q={{key}}",
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
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        clean_path.write_text(
            json.dumps(
                [
                    {
                        "name": "配置净化规则",
                        "pattern": "广告",
                        "replacement": "",
                        "isEnabled": True,
                        "scopeContent": True,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        plugin = self.module.JsonlNovelDownloaderPlugin(
            context=object(),
            config={
                "book_sources": [str(source_path)],
                "clean_rule_sources": [str(clean_path)],
            },
        )
        self.assertTrue(plugin.wait_for_bootstrap(2.0))

        sources = plugin.source_registry.list_sources()
        clean_repos = plugin.clean_rule_store.list_repositories()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "配置书源")
        self.assertEqual(len(clean_repos), 1)
        self.assertEqual(clean_repos[0]["rule_count"], 1)

    def test_plugin_bootstrap_runs_in_background(self):
        source_path = self.base_dir / "slow-bootstrap-source.json"
        source_path.write_text(
            json.dumps(
                [
                    {
                        "bookSourceName": "慢启动配置书源",
                        "bookSourceUrl": "https://example.com",
                        "searchUrl": "https://example.com/search?q={{key}}",
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
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        plugin_base = importlib.import_module("astrbot_plugin_webnovel_downloader.plugin_base")
        original_loader = plugin_base.load_text_argument
        started = threading.Event()
        unblock = threading.Event()

        def slow_loader(*args, **kwargs):
            started.set()
            unblock.wait(1.0)
            return original_loader(*args, **kwargs)

        plugin_base.load_text_argument = slow_loader
        plugin = None
        try:
            begin = time.perf_counter()
            plugin = self.module.JsonlNovelDownloaderPlugin(
                context=object(),
                config={"book_sources": [str(source_path)]},
            )
            elapsed = time.perf_counter() - begin
            self.assertLess(elapsed, 0.2)
            self.assertTrue(started.wait(1.0))
            self.assertEqual(plugin.source_registry.list_sources(), [])

            unblock.set()
            self.assertTrue(plugin.wait_for_bootstrap(2.0))
            self.assertEqual(len(plugin.source_registry.list_sources()), 1)
        finally:
            unblock.set()
            plugin_base.load_text_argument = original_loader
            if plugin is not None:
                plugin.wait_for_bootstrap(2.0)

    def test_plugin_bootstrap_skips_successful_duplicate_config_imports(self):
        source_path = self.base_dir / "bootstrap-skip-source.json"
        source_path.write_text(
            json.dumps(
                [
                    {
                        "bookSourceName": "去重配置书源",
                        "bookSourceUrl": "https://example.com",
                        "searchUrl": "https://example.com/search?q={{key}}",
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
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        plugin = self.module.JsonlNovelDownloaderPlugin(
            context=object(),
            config={"book_sources": [str(source_path)]},
        )
        self.assertTrue(plugin.wait_for_bootstrap(2.0))
        self.assertEqual(len(plugin.source_registry.list_sources()), 1)

        plugin_base = importlib.import_module("astrbot_plugin_webnovel_downloader.plugin_base")
        original_loader = plugin_base.load_text_argument

        def should_not_run(*args, **kwargs):
            raise AssertionError("重复配置导入不应再次请求 load_text_argument")

        plugin_base.load_text_argument = should_not_run
        try:
            plugin_again = self.module.JsonlNovelDownloaderPlugin(
                context=object(),
                config={"book_sources": [str(source_path)]},
            )
            self.assertTrue(plugin_again.wait_for_bootstrap(0.1))
            self.assertEqual(len(plugin_again.source_registry.list_sources()), 1)
        finally:
            plugin_base.load_text_argument = original_loader

    async def test_download_status_offloads_blocking_status_read(self):
        original_get_status = self.plugin.manager.get_status

        def slow_get_status(job_id):
            time.sleep(0.2)
            return {
                "job_id": job_id,
                "book_name": "阻塞测试",
                "state": "created",
                "total_chapters": 1,
                "completed_chapters": 0,
                "failed_chapters": 0,
                "missing_chapters": 1,
                "output_filename": "test.txt",
                "output_path": "/tmp/test.txt",
                "journal_path": "/tmp/test.jsonl",
                "latest_errors": [],
                "corrupt_lines": 0,
            }

        self.plugin.manager.get_status = slow_get_status
        try:
            start = time.perf_counter()
            task = asyncio.create_task(self.plugin.handle_novel_download_status("job-1"))
            await asyncio.sleep(0.01)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.1)
            self.assertFalse(task.done())
            result = await task
            self.assertIn("任务状态: job-1", result)
        finally:
            self.plugin.manager.get_status = original_get_status

    async def test_import_sources_offloads_blocking_render(self):
        original_render = self.plugin.renderer.render_import_summary
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "阻塞渲染测试源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": "https://example.com/search?q={{key}}",
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
            ],
            ensure_ascii=False,
        )

        def slow_render(result):
            time.sleep(0.2)
            return original_render(result)

        self.plugin.renderer.render_import_summary = slow_render
        try:
            start = time.perf_counter()
            task = asyncio.create_task(self.plugin.handle_novel_import_sources(source_json))
            await asyncio.sleep(0.01)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.1)
            self.assertFalse(task.done())
            result = await task
            payload = json.loads(result)
            self.assertEqual(payload["imported_count"], 1)
        finally:
            self.plugin.renderer.render_import_summary = original_render

    def test_search_service_respects_time_budget_and_returns_partial_results(self):
        search_module = importlib.import_module(
            "astrbot_plugin_webnovel_downloader.core.search_service"
        )

        class FakeRegistry(object):
            def load_enabled_source_summaries(self, source_ids=None, include_disabled=False):
                return [
                    {
                        "source_id": "fast",
                        "name": "快源",
                        "supports_search": True,
                    },
                    {
                        "source_id": "slow-a",
                        "name": "慢源A",
                        "supports_search": True,
                    },
                    {
                        "source_id": "slow-b",
                        "name": "慢源B",
                        "supports_search": True,
                    },
                ]

            def load_enabled_sources(self, source_ids=None, include_disabled=False):
                return [
                    {"source_id": "fast", "name": "快源"},
                    {"source_id": "slow-a", "name": "慢源A"},
                    {"source_id": "slow-b", "name": "慢源B"},
                ]

        class FakeEngine(object):
            def search_books(self, source, keyword, limit):
                if source["source_id"] == "fast":
                    return [
                        {
                            "source_id": source["source_id"],
                            "source_name": source["name"],
                            "title": keyword,
                            "author": "测试作者",
                            "book_url": "https://example.com/book",
                        }
                    ]
                time.sleep(0.2)
                return []

        service = search_module.SearchService(
            FakeRegistry(),
            FakeEngine(),
            search_module.SearchServiceConfig(max_workers=3, time_budget_seconds=0.05),
        )

        start = time.perf_counter()
        result = service.search("诡秘之主", limit=3)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.15)
        self.assertTrue(result["partial"])
        self.assertGreaterEqual(result["timed_out_source_count"], 1)
        self.assertEqual(result["completed_sources"], 1)
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["title"], "诡秘之主")

    def test_plugin_init_rejects_non_positive_search_time_budget(self):
        with self.assertRaisesRegex(ValueError, "search_time_budget.*必须大于 0"):
            self.module.JsonlNovelDownloaderPlugin(
                context=object(),
                config={"search_time_budget": 0},
            )

    async def test_search_cache_can_list_and_download_result(self):
        chapters_dir = self.base_dir / "cache-chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "1.html").write_text(
            "<html><body><h1>第一章</h1><div id='content'>缓存下载第一章</div></body></html>",
            encoding="utf-8",
        )
        (chapters_dir / "2.html").write_text(
            "<html><body><h1>第二章</h1><div id='content'>缓存下载第二章</div></body></html>",
            encoding="utf-8",
        )
        (self.base_dir / "cache-book.html").write_text(
            "<html><body>"
            "<h1>缓存小说</h1>"
            "<div class='author'>缓存作者</div>"
            "<div id='toc'>"
            "<a href='{c1}'>第一章</a>"
            "<a href='{c2}'>第二章</a>"
            "</div>"
            "</body></html>".format(
                c1=(chapters_dir / "1.html").resolve().as_uri(),
                c2=(chapters_dir / "2.html").resolve().as_uri(),
            ),
            encoding="utf-8",
        )
        (self.base_dir / "cache-search.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "缓存小说",
                                "author": "缓存作者",
                                "url": (self.base_dir / "cache-book.html").resolve().as_uri(),
                                "intro": "缓存简介",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "缓存测试源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "cache-search.json").resolve().as_uri(),
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
                    },
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "title": "h1&&text",
                        "content": "#content&&text",
                    },
                }
            ],
            ensure_ascii=False,
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        search_result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "缓存小说"))
        search_id = search_result["search_id"]
        self.assertTrue(search_id)
        self.assertEqual(search_result["results"][0]["result_index"], 0)

        searches = json.loads(await self._invoke_tool(self.plugin.novel_list_searches, "10", "0"))
        self.assertEqual(searches["total_count"], 1)
        self.assertEqual(searches["searches"][0]["search_id"], search_id)

        cached_page = json.loads(
            await self._invoke_tool(
                self.plugin.novel_get_search_results,
                search_id,
                "10",
                "0",
            )
        )
        self.assertEqual(cached_page["total_result_count"], 1)
        self.assertEqual(cached_page["results"][0]["title"], "缓存小说")

        download_text = await self._invoke_tool(
            self.plugin.novel_download_search_result,
            search_id,
            "0",
            "",
            "true",
        )
        self.assertIn("已创建并启动任务", download_text)
        job_id = download_text.splitlines()[0].split(": ", 1)[1]
        await self.plugin._running_tasks[job_id]
        status_text = await self._invoke_tool(self.plugin.novel_download_status, job_id)
        self.assertIn("状态: assembled", status_text)
        output_path = self.plugin.manager.output_dir / "缓存小说.txt"
        self.assertTrue(output_path.exists())
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("缓存下载第一章", content)
        self.assertIn("缓存下载第二章", content)

    async def test_import_clean_rules_repo_applies_to_downloaded_content(self):
        chapters_dir = self.base_dir / "clean-repo-chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "1.html").write_text(
            "<html><body><h1>第一章</h1><div id='content'>正文保留 站点广告 继续保留</div></body></html>",
            encoding="utf-8",
        )
        (self.base_dir / "clean-repo-book.html").write_text(
            "<html><body>"
            "<h1>净化仓库测试书</h1>"
            "<div class='author'>净化作者</div>"
            "<div id='toc'>"
            "<a href='{c1}'>第一章</a>"
            "</div>"
            "</body></html>".format(
                c1=(chapters_dir / "1.html").resolve().as_uri(),
            ),
            encoding="utf-8",
        )
        (self.base_dir / "clean-repo-search.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "净化仓库测试书",
                                "author": "净化作者",
                                "url": (self.base_dir / "clean-repo-book.html").resolve().as_uri(),
                                "intro": "正文净化测试",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "净化仓库测试源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "clean-repo-search.json").resolve().as_uri(),
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
                    },
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "title": "h1&&text",
                        "content": "#content&&text",
                    },
                }
            ],
            ensure_ascii=False,
        )
        clean_repo_json = json.dumps(
            [
                {
                    "name": "移除站点广告",
                    "group": "test",
                    "pattern": "站点广告",
                    "replacement": "",
                    "isRegex": False,
                    "scope": "净化仓库测试源",
                }
            ],
            ensure_ascii=False,
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        import_result = json.loads(
            await self._invoke_tool(
                self.plugin.novel_import_clean_rules,
                clean_repo_json,
                "测试净化仓库",
            )
        )
        self.assertEqual(import_result["name"], "测试净化仓库")
        self.assertEqual(import_result["rule_count"], 1)
        self.assertEqual(import_result["scoped_rule_count"], 1)
        self.assertTrue(Path(import_result["path"]).exists())

        repo_list = json.loads(await self._invoke_tool(self.plugin.novel_list_clean_rules, "10", "0"))
        self.assertEqual(repo_list["total_count"], 1)
        self.assertEqual(repo_list["repositories"][0]["name"], "测试净化仓库")

        search_result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "净化仓库测试书"))
        download_text = await self._invoke_tool(
            self.plugin.novel_download_search_result,
            search_result["search_id"],
            "0",
            "",
            "true",
        )
        self.assertIn("已创建并启动任务", download_text)
        job_id = download_text.splitlines()[0].split(": ", 1)[1]
        await self.plugin._running_tasks[job_id]
        output_path = self.plugin.manager.output_dir / "净化仓库测试书.txt"
        self.assertTrue(output_path.exists())
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("正文保留", content)
        self.assertIn("继续保留", content)
        self.assertNotIn("站点广告", content)

    async def test_import_clean_rules_skips_js_and_title_only_rules(self):
        clean_repo_json = json.dumps(
            [
                {
                    "name": "JS规则",
                    "pattern": "广告",
                    "replacement": "@js:return '';",
                    "isRegex": True,
                    "scopeContent": True,
                },
                {
                    "name": "标题规则",
                    "pattern": "第",
                    "replacement": "",
                    "isRegex": True,
                    "scopeTitle": True,
                    "scopeContent": False,
                },
                {
                    "name": "正文规则",
                    "pattern": "尾注",
                    "replacement": "",
                    "isRegex": False,
                    "scopeContent": True,
                },
            ],
            ensure_ascii=False,
        )
        result = json.loads(
            await self._invoke_tool(
                self.plugin.novel_import_clean_rules,
                clean_repo_json,
                "跳过测试仓库",
            )
        )
        self.assertEqual(result["rule_count"], 1)
        self.assertEqual(result["skipped_rule_count"], 2)

        repo_list = json.loads(await self._invoke_tool(self.plugin.novel_list_clean_rules, "10", "0"))
        self.assertEqual(repo_list["repositories"][0]["rule_count"], 1)
        self.assertEqual(repo_list["repositories"][0]["skipped_rule_count"], 2)

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
        self.assertTrue(result["search_id"])
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

    async def test_search_supports_json_template_fields_in_result_rules(self):
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "模板字段搜索源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "search-template.json").resolve().as_uri(),
                    "ruleSearch": {
                        "bookList": "[*]",
                        "name": "name",
                        "author": "author",
                        "bookUrl": "/detail?bookid={{$.bid}}",
                        "wordCount": "{{$.words}}字",
                        "intro": "summary",
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
        (self.base_dir / "search-template.json").write_text(
            json.dumps(
                [
                    {
                        "bid": 1010868264,
                        "name": "诡秘之主",
                        "author": "爱潜水的乌贼",
                        "words": 4465200,
                        "summary": "蒸汽与机械的浪潮中……",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "诡秘之主"))
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(
            result["results"][0]["book_url"],
            "https://example.com/detail?bookid=1010868264",
        )
        self.assertEqual(result["results"][0]["word_count"], "4465200字")

    async def test_search_supports_legado_html_chain_and_index_steps(self):
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "HTML链式搜索源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "search-chain.html").resolve().as_uri(),
                    "ruleSearch": {
                        "bookList": ".mybook@.hot_sale",
                        "name": "p.0@text",
                        "author": "p.1@text##\\s*\\|.*##",
                        "kind": "p.1@text##.*\\|\\s*##",
                        "lastChapter": "p.2@text##连载 \\| 更新：|(\\|)",
                        "bookUrl": "a.0@href",
                        "coverUrl": "img@src",
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
        (self.base_dir / "search-chain.html").write_text(
            "<html><body>"
            "<div class='mybook'>"
            "<div class='hot_sale'>"
            "<p>诡秘之主</p>"
            "<p>爱潜水的乌贼 | 玄幻</p>"
            "<p>连载 | 更新：第一千二百章</p>"
            "<a href='/books/1'>详情</a>"
            "<a href='/books/alt'>备用</a>"
            "<img src='/covers/1.jpg'/>"
            "</div>"
            "</div>"
            "</body></html>",
            encoding="utf-8",
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        result = json.loads(await self._invoke_tool(self.plugin.novel_search_books, "诡秘之主"))
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["successful_sources"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["results"][0]["title"], "诡秘之主")
        self.assertIn("爱潜水的乌贼", result["results"][0]["author"])
        self.assertEqual(result["results"][0]["kind"], "玄幻")
        self.assertEqual(result["results"][0]["book_url"], "https://example.com/books/1")

    async def test_build_plan_supports_css_attr_value_and_current_node_template(self):
        toc_path = self.base_dir / "attr-toc.html"
        chapter_path = self.base_dir / "attr-chapter.html"
        detail_path = self.base_dir / "attr-detail.html"

        chapter_path.write_text(
            "<html><body><div id='content'>正文</div></body></html>",
            encoding="utf-8",
        )
        toc_path.write_text(
            "<html><body><div id='toc'><a href='{chapter}'>第一章</a></div></body></html>".format(
                chapter=chapter_path.resolve().as_uri()
            ),
            encoding="utf-8",
        )
        detail_path.write_text(
            "<html><head>"
            "<meta property='og:novel:book_name' content='兼容测试书' />"
            "<meta property='og:novel:author' content='测试作者' />"
            "<meta property='og:novel:update_time' content='2026-04-17' />"
            "</head><body>"
            "<a id='toc-link' href='{toc}'>目录</a>"
            "</body></html>".format(toc=toc_path.resolve().as_uri()),
            encoding="utf-8",
        )

        source_json = json.dumps(
            [
                {
                    "bookSourceName": "属性选择器兼容源",
                    "bookSourceUrl": "https://example.com",
                    "ruleBookInfo": {
                        "name": "[property=og:novel:book_name]@content",
                        "author": "[property=og:novel:author]@content",
                        "intro": "更新时间：{{@@[property=\"og:novel:update_time\"]@content##-##/}}",
                        "tocUrl": "#toc-link@href",
                    },
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

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(sources[0]["source_id"])
        plan = self.plugin.search_service.engine.build_book_download_plan(
            source,
            detail_path.resolve().as_uri(),
            "",
        )

        self.assertEqual(plan["book_name"], "兼容测试书")
        self.assertEqual(plan["author"], "测试作者")
        self.assertIn("2026/04/17", plan["intro"])
        self.assertEqual(plan["toc"][0]["title"], "第一章")

    async def test_fetch_chapter_content_supports_text_paging_multi_node_and_replace_regex(self):
        chapter_page_2 = self.base_dir / "chapter-page-2.html"
        chapter_page_1 = self.base_dir / "chapter-page-1.html"

        chapter_page_2.write_text(
            "<html><body>"
            "<div class='chaptercontent'>"
            "<p>第二页标记 (第2/2页)</p>"
            "<p>正文B</p>"
            "</div>"
            "</body></html>",
            encoding="utf-8",
        )
        chapter_page_1.write_text(
            "<html><body>"
            "<div class='chaptercontent'>"
            "<p>第一页标记 (第1/2页)</p>"
            "<p>正文A</p>"
            "</div>"
            "<a href='{page2}'>下一页</a>"
            "</body></html>".format(page2=chapter_page_2.resolve().as_uri()),
            encoding="utf-8",
        )

        source_json = json.dumps(
            [
                {
                    "bookSourceName": "正文分页兼容源",
                    "bookSourceUrl": "https://example.com",
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "content": ".chaptercontent@p@html",
                        "nextContentUrl": "text.下一页@href",
                        "replaceRegex": "##\\(第\\d+/\\d+页\\)##",
                    },
                }
            ],
            ensure_ascii=False,
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(sources[0]["source_id"])
        chapter = self.plugin.search_service.engine.fetch_chapter_content(
            source,
            chapter_page_1.resolve().as_uri(),
            "测试章节",
        )

        self.assertIn("正文A", chapter["content"])
        self.assertIn("正文B", chapter["content"])
        self.assertNotIn("<p>", chapter["content"])
        self.assertNotIn("(第1/2页)", chapter["content"])
        self.assertNotIn("(第2/2页)", chapter["content"])

    async def test_fetch_chapter_content_removes_generic_page_markers_without_replace_regex(self):
        chapter_page_2 = self.base_dir / "chapter-generic-page-2.html"
        chapter_page_1 = self.base_dir / "chapter-generic-page-1.html"

        chapter_page_2.write_text(
            "<html><body>"
            "<div class='chaptercontent'>"
            "<p>第二段（第2/2页）</p>"
            "<p>正文B</p>"
            "</div>"
            "</body></html>",
            encoding="utf-8",
        )
        chapter_page_1.write_text(
            "<html><body>"
            "<div class='chaptercontent'>"
            "<p>第一段 (第1/2页)</p>"
            "<p>正文A</p>"
            "</div>"
            "<a href='{page2}'>下一页</a>"
            "</body></html>".format(page2=chapter_page_2.resolve().as_uri()),
            encoding="utf-8",
        )

        source_json = json.dumps(
            [
                {
                    "bookSourceName": "正文通用分页清洗源",
                    "bookSourceUrl": "https://example.com",
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "content": ".chaptercontent@p@html",
                        "nextContentUrl": "text.下一页@href",
                    },
                }
            ],
            ensure_ascii=False,
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(sources[0]["source_id"])
        chapter = self.plugin.search_service.engine.fetch_chapter_content(
            source,
            chapter_page_1.resolve().as_uri(),
            "测试章节",
        )

        self.assertIn("正文A", chapter["content"])
        self.assertIn("正文B", chapter["content"])
        self.assertNotIn("(第1/2页)", chapter["content"])
        self.assertNotIn("（第2/2页）", chapter["content"])

    async def test_build_plan_follows_more_than_five_toc_pages(self):
        chapter_pages = []
        toc_pages = []
        for index in range(1, 8):
            chapter_path = self.base_dir / "toc-many-chapter-{index}.html".format(index=index)
            chapter_path.write_text(
                "<html><body><div id='content'>正文{index}</div></body></html>".format(index=index),
                encoding="utf-8",
            )
            chapter_pages.append(chapter_path)

        for page_no in range(1, 8):
            toc_path = self.base_dir / "toc-many-page-{page}.html".format(page=page_no)
            next_link = ""
            if page_no < 7:
                next_link = "<a href='{href}'>下一页</a>".format(
                    href=(self.base_dir / "toc-many-page-{page}.html".format(page=page_no + 1))
                    .resolve()
                    .as_uri()
                )
            toc_path.write_text(
                "<html><body>"
                "<div id='toc'>"
                "<a href='{chapter}'>第{index}章</a>"
                "</div>"
                "{next_link}"
                "</body></html>".format(
                    chapter=chapter_pages[page_no - 1].resolve().as_uri(),
                    index=page_no,
                    next_link=next_link,
                ),
                encoding="utf-8",
            )
            toc_pages.append(toc_path)

        detail_path = self.base_dir / "toc-many-detail.html"
        detail_path.write_text(
            "<html><body><h1>多页目录测试书</h1><a id='toc-link' href='{toc}'>目录</a></body></html>".format(
                toc=toc_pages[0].resolve().as_uri()
            ),
            encoding="utf-8",
        )

        source_json = json.dumps(
            [
                {
                    "bookSourceName": "多页目录兼容源",
                    "bookSourceUrl": "https://example.com",
                    "ruleBookInfo": {
                        "name": "h1&&text",
                        "tocUrl": "#toc-link@href",
                    },
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                        "nextTocUrl": "text.下一页@href",
                    },
                    "ruleContent": {"content": "#content&&text"},
                }
            ],
            ensure_ascii=False,
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(sources[0]["source_id"])
        plan = self.plugin.search_service.engine.build_book_download_plan(
            source,
            detail_path.resolve().as_uri(),
            "",
        )

        self.assertEqual(len(plan["toc"]), 7)
        self.assertEqual(plan["toc"][-1]["title"], "第7章")

    async def test_fetch_chapter_content_removes_duplicate_leading_title(self):
        chapter_page = self.base_dir / "chapter-duplicate-title.html"
        chapter_page.write_text(
            "<html><body>"
            "<h1>第一章 测试标题</h1>"
            "<div id='content'>"
            "<p>第一章 测试标题</p>"
            "<p>这里是正文第一段。</p>"
            "<p>第一章 测试标题</p>"
            "<p>这里是正文第二段。</p>"
            "</div>"
            "</body></html>",
            encoding="utf-8",
        )

        source_json = json.dumps(
            [
                {
                    "bookSourceName": "重复标题清洗源",
                    "bookSourceUrl": "https://example.com",
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "title": "h1&&text",
                        "content": "#content@p@html",
                    },
                }
            ],
            ensure_ascii=False,
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(sources[0]["source_id"])
        chapter = self.plugin.search_service.engine.fetch_chapter_content(
            source,
            chapter_page.resolve().as_uri(),
            "第一章 测试标题",
        )

        stripped_lines = [line.strip() for line in chapter["content"].splitlines()]
        self.assertNotIn("第一章 测试标题", stripped_lines)
        self.assertIn("这里是正文第一段。", chapter["content"])
        self.assertIn("这里是正文第二段。", chapter["content"])
        self.assertTrue(chapter["content"].splitlines()[0].startswith("\u3000\u3000"))

    async def test_fetch_chapter_content_formats_chinese_paragraphs_and_merges_broken_page_lines(self):
        chapter_page = self.base_dir / "chapter-formatting.html"
        chapter_page.write_text(
            "<html><body>"
            "<div id='content'>"
            "<p>不过我们也是你们的敌人。</p>"
            "<p>但是</p>"
            "<p>啊，今天有我在，卡洛普学院遗址的“宝藏”，你们别想抢走了。</p>"
            "<p>第二段也应该保留。</p>"
            "</div>"
            "</body></html>",
            encoding="utf-8",
        )

        source_json = json.dumps(
            [
                {
                    "bookSourceName": "中文排版格式化源",
                    "bookSourceUrl": "https://example.com",
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {"content": "#content@p@html"},
                }
            ],
            ensure_ascii=False,
        )

        await self._invoke_tool(self.plugin.novel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(sources[0]["source_id"])
        chapter = self.plugin.search_service.engine.fetch_chapter_content(
            source,
            chapter_page.resolve().as_uri(),
            "测试章节",
        )

        lines = [line for line in chapter["content"].splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith("\u3000\u3000") for line in lines))
        self.assertEqual(lines[0].strip(), "不过我们也是你们的敌人。")
        self.assertIn("但是啊，今天有我在", lines[1])
        self.assertNotIn("\n\n", chapter["content"])
        self.assertEqual(lines[2].strip(), "第二段也应该保留。")

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
