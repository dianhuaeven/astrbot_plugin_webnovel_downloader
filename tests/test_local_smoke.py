from __future__ import annotations

import asyncio
import inspect
import importlib
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import get_args, get_origin
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
        self.plugin_dir = Path(__file__).resolve().parents[1]
        self.package_name = self.plugin_dir.name
        self.package_parent = str(self.plugin_dir.parent)
        self._inserted_sys_path = False
        if self.package_parent not in sys.path:
            sys.path.insert(0, self.package_parent)
            self._inserted_sys_path = True

        self._install_astrbot_stubs()
        self.module = importlib.import_module(
            "{name}.main".format(name=self.package_name)
        )
        self._managed_plugins = []
        self.plugin = self._create_plugin()
        self.addAsyncCleanup(self._terminate_managed_plugins)

    def tearDown(self):
        for plugin in reversed(getattr(self, "_managed_plugins", [])):
            wait_for_bootstrap = getattr(plugin, "wait_for_bootstrap", None)
            if callable(wait_for_bootstrap):
                wait_for_bootstrap(5.0)
            shutdown_probe = getattr(
                getattr(plugin, "source_probe_service", None), "shutdown", None
            )
            if callable(shutdown_probe):
                shutdown_probe(5.0)
        for name in list(sys.modules):
            if name.startswith("astrbot"):
                sys.modules.pop(name, None)
        for name in list(sys.modules):
            if name == self.package_name or name.startswith(
                "{name}.".format(name=self.package_name)
            ):
                sys.modules.pop(name, None)
        if self._inserted_sys_path:
            try:
                sys.path.remove(self.package_parent)
            except ValueError:
                pass
        self.tempdir.cleanup()

    def _create_plugin(self, config=None):
        test_config = {
            "allow_unsafe_urls": True,
            "auto_probe_on_import": False,
            "download_sample_chapters": 1,
            "download_sample_min_chars": 1,
        }
        test_config.update(dict(config or {}))
        plugin = self.module.JsonlNovelDownloaderPlugin(
            context=object(), config=test_config
        )
        self._managed_plugins.append(plugin)
        return plugin

    async def _terminate_managed_plugins(self):
        for plugin in reversed(getattr(self, "_managed_plugins", [])):
            terminate = getattr(plugin, "terminate", None)
            if inspect.iscoroutinefunction(terminate):
                await terminate()
                continue
            shutdown_probe = getattr(
                getattr(plugin, "source_probe_service", None), "shutdown", None
            )
            if callable(shutdown_probe):
                shutdown_probe(1.0)

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
            class PermissionType(object):
                ADMIN = "admin"

            @staticmethod
            def command(_name):
                def decorator(func):
                    return func

                return decorator

            @staticmethod
            def permission_type(permission):
                def decorator(func):
                    func.__permission_type__ = permission
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
            unified_msg_origin = "aiocqhttp:FriendMessage:42"
            role = "member"

            def get_sender_id(self):
                return "42"

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
            def _decode_form_keyword(
                self, text: str, field_name: str, encoding: str
            ) -> str:
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
                self.wfile.write(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")
                )

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
                self.wfile.write(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")
                )

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.shutdown)
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
        tool_attrs = {}
        for attr_name in dir(self.plugin):
            attr = getattr(self.plugin, attr_name)
            tool_name = getattr(attr, "__llm_tool_name__", "")
            if tool_name:
                tool_names.add(tool_name)
                tool_attrs[tool_name] = attr

        self.assertEqual(
            tool_names,
            {
                "webnovel_search_books",
                "webnovel_download_book",
                "webnovel_download_status",
                "webnovel_import_sources",
                "webnovel_list_sources",
                "webnovel_refresh_sources",
                "webnovel_probe_status",
                "webnovel_import_clean_rules",
                "webnovel_list_clean_rules",
            },
        )
        for old_tool_name in (
            "novel_enable_source",
            "novel_resume_book_download",
            "novel_resume_download",
            "novel_auto_download",
            "novel_search_books",
            "novel_list_searches",
            "novel_get_search_results",
            "novel_download_search_result",
            "novel_download_book",
            "novel_fetch_preview",
            "novel_start_download",
            "novel_assemble_book",
            "novel_list_jobs",
            "novel_download",
            "novel_remove_source",
            "novel_get_source_detail",
            "novel_query_candidates",
            "novel_inspect_source_book",
            "novel_download_source_book",
            "novel_read_search_results",
            "novel_download_cached_result",
            "novel_download_status",
            "webnovel_fetch_preview",
        ):
            self.assertNotIn(old_tool_name, tool_names)
        self.assertFalse(hasattr(self.plugin, "webnovel_fetch_preview"))
        for admin_tool_name in (
            "webnovel_import_clean_rules",
            "webnovel_import_sources",
            "webnovel_refresh_sources",
        ):
            self.assertTrue(
                getattr(tool_attrs[admin_tool_name], "__admin_only__", False)
            )
            self.assertEqual(
                getattr(tool_attrs[admin_tool_name], "__permission_type__", ""),
                "admin",
            )

    def test_llm_tool_signature_hides_system_event_parameter(self):
        signature = inspect.signature(self.plugin.webnovel_search_books)
        self.assertNotIn("event", signature.parameters)
        self.assertEqual(
            list(signature.parameters),
            ["keyword", "author", "limit", "include_disabled"],
        )

    def test_search_tool_doc_requires_confirmed_download_target(self):
        doc = inspect.getdoc(self.plugin.webnovel_search_books) or ""
        self.assertIn("不是通用搜索、推荐、探索", doc)
        self.assertIn("明确要求下载完整小说", doc)
        self.assertIn("必须先向用户确认作者", doc)

    async def test_llm_tool_accepts_runtime_call_without_event_argument(self):
        recorded = {}

        async def fake_handle(keyword, author="", limit="", include_disabled=""):
            recorded["keyword"] = keyword
            recorded["author"] = author
            recorded["limit"] = limit
            recorded["include_disabled"] = include_disabled
            return "ok"

        self.plugin.handle_webnovel_search_books = fake_handle
        result = await self.plugin.webnovel_search_books("瘟疫医生", limit="5")

        self.assertEqual(result, "ok")
        self.assertEqual(
            recorded,
            {
                "keyword": "瘟疫医生",
                "author": "",
                "limit": "5",
                "include_disabled": "",
            },
        )

    async def test_webnovel_download_book_accepts_runtime_call_without_event_argument(
        self,
    ):
        recorded = {}

        async def fake_handle(
            search_id,
            group_index,
            attempt_limit="",
            output_filename="",
            auto_assemble="true",
            skip_source_ids="",
        ):
            recorded["search_id"] = search_id
            recorded["group_index"] = group_index
            recorded["attempt_limit"] = attempt_limit
            recorded["output_filename"] = output_filename
            recorded["auto_assemble"] = auto_assemble
            recorded["skip_source_ids"] = skip_source_ids
            return "ok"

        self.plugin.handle_webnovel_download_book = fake_handle
        result = await self.plugin.webnovel_download_book(
            "search-1",
            "2",
            attempt_limit="3",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(
            recorded,
            {
                "search_id": "search-1",
                "group_index": "2",
                "attempt_limit": "3",
                "output_filename": "",
                "auto_assemble": "true",
                "skip_source_ids": "",
            },
        )

    async def test_webnovel_download_book_forwards_runtime_event_for_notification(self):
        recorded = {}

        async def fake_handle(
            search_id,
            group_index,
            attempt_limit="",
            output_filename="",
            auto_assemble="true",
            skip_source_ids="",
            event=None,
        ):
            recorded["event"] = event
            return "ok"

        self.plugin.handle_webnovel_download_book = fake_handle
        event = sys.modules["astrbot.api.event"].AstrMessageEvent()

        result = await self.plugin.webnovel_download_book(event, "search-1", "0")

        self.assertEqual(result, "ok")
        self.assertIs(recorded["event"], event)

    def test_download_callback_without_event_is_disabled_without_storage(self):
        result = self.plugin._register_download_callback(
            None,
            "job-no-event",
            "run-no-event",
            {"job": {"job_id": "job-no-event"}},
        )

        self.assertFalse(result["enabled"])
        self.assertEqual(result["reason"], "no AstrBot event context")
        self.assertFalse(self.plugin._download_callback_path.exists())

    def test_skip_source_ids_parser_supports_all_documented_forms(self):
        self.assertEqual(self.plugin._parse_string_list("source-a"), ["source-a"])
        self.assertEqual(
            self.plugin._parse_string_list("source-a,source-b，source-c\nsource-a"),
            ["source-a", "source-b", "source-c"],
        )
        self.assertEqual(
            self.plugin._parse_string_list('["source-a", "source-b", "source-a"]'),
            ["source-a", "source-b"],
        )

    def test_candidate_filter_uses_skip_registry_and_health_at_download_time(self):
        class FakeRegistry(object):
            def get_source_summary(self, source_id):
                summaries = {
                    "source-a": {"enabled": True, "supports_download": True},
                    "source-b": {"enabled": True, "supports_download": True},
                    "source-c": {"enabled": False, "supports_download": True},
                    "source-d": {"enabled": True, "supports_download": True},
                }
                if source_id not in summaries:
                    raise ValueError("missing")
                return summaries[source_id]

        class FakeHealthStore(object):
            def get_many(self, _source_ids):
                return {
                    "source-b": {
                        "download": {
                            "state": "unsupported",
                            "note": "运行时不支持",
                        }
                    }
                }

        self.plugin.source_registry = FakeRegistry()
        self.plugin.source_health_store = FakeHealthStore()
        candidates = [
            {
                "source_id": source_id,
                "source_name": source_id,
                "book_url": "https://example.com/{source_id}".format(
                    source_id=source_id
                ),
                "supports_download": True,
            }
            for source_id in ("source-a", "source-b", "source-c", "source-d", "deleted")
        ]

        filtered = self.plugin._filter_safe_candidate_group(
            {"candidates": candidates}, ["source-a"]
        )

        self.assertEqual(
            [item["source_id"] for item in filtered["candidates"]], ["source-d"]
        )
        skipped = {
            item["source_id"]: item["skip_reason"]
            for item in filtered["skipped_candidates"]
        }
        self.assertIn("用户本次要求跳过", skipped["source-a"])
        self.assertIn("运行时不支持", skipped["source-b"])
        self.assertIn("禁用", skipped["source-c"])
        self.assertIn("不在当前注册表", skipped["deleted"])

    async def test_download_finished_notification_falls_back_direct_once(self):
        self.plugin.notify_mode = "llm"
        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:1000",
            role="member",
            get_sender_id=lambda: "42",
        )
        run_id = "run-1"
        result = self.plugin._register_download_callback(
            event,
            "job-notify-1",
            run_id,
            {
                "selected": {"title": "通知测试书", "source_id": "source-1"},
                "job": {
                    "job_id": "job-notify-1",
                    "book_name": "通知测试书",
                    "source_id": "source-1",
                    "source_name": "测试源",
                },
            },
        )
        self.assertEqual(result["run_id"], run_id)

        def fake_get_status(job_id):
            self.assertEqual(job_id, "job-notify-1")
            return {
                "job_id": job_id,
                "state": "downloaded",
                "book_name": "通知测试书",
                "completed_chapters": 2,
                "total_chapters": 2,
                "output_filename": "通知测试书.txt",
            }

        sent = []

        async def fake_wake(callback, status, text):
            sent.append(("wake", callback, status, text))
            return False

        async def fake_direct(callback, text):
            sent.append(("direct", callback, {}, text))
            return True

        self.plugin.manager.get_status = fake_get_status
        self.plugin._wake_llm_for_download_result = fake_wake
        self.plugin._send_direct_download_notification = fake_direct

        await self.plugin._notify_download_finished("job-notify-1", run_id)
        await self.plugin._notify_download_finished("job-notify-1", run_id)

        self.assertEqual([item[0] for item in sent], ["wake", "direct"])
        self.assertIn("下载完成", sent[-1][3])
        payload = json.loads(self.plugin._download_callback_path.read_text("utf-8"))
        self.assertEqual(payload["callbacks"], {})

    async def test_download_finished_notification_prefers_llm_without_direct_fallback(
        self,
    ):
        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:42",
            role="member",
            get_sender_id=lambda: "42",
        )
        self.plugin._register_download_callback(
            event,
            "job-llm-success",
            "run-llm-success",
            {"job": {"job_id": "job-llm-success", "book_name": "LLM 通知书"}},
        )
        self.plugin.manager.get_status = lambda _job_id: {
            "job_id": "job-llm-success",
            "state": "assembled",
            "book_name": "LLM 通知书",
            "completed_chapters": 1,
            "total_chapters": 1,
        }
        calls = []

        async def fake_wake(callback, status, text):
            calls.append(("llm", callback["run_id"], status["state"], text))
            return True

        async def fail_direct(_callback, _text):
            raise AssertionError("LLM wake 成功后不应调用 direct fallback")

        self.plugin._wake_llm_for_download_result = fake_wake
        self.plugin._send_direct_download_notification = fail_direct

        await self.plugin._notify_download_finished(
            "job-llm-success", "run-llm-success"
        )
        await self.plugin._notify_download_finished(
            "job-llm-success", "run-llm-success"
        )

        self.assertEqual([item[0] for item in calls], ["llm"])
        payload = json.loads(self.plugin._download_callback_path.read_text("utf-8"))
        self.assertEqual(payload["callbacks"], {})

    async def test_notification_double_failure_is_persisted_without_auto_retry(self):
        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:42",
            role="member",
            get_sender_id=lambda: "42",
        )
        self.plugin._register_download_callback(
            event,
            "job-delivery-failed",
            "run-delivery-failed",
            {"job": {"job_id": "job-delivery-failed", "book_name": "失败通知书"}},
        )
        self.plugin.manager.get_status = lambda _job_id: {
            "job_id": "job-delivery-failed",
            "state": "failed",
            "book_name": "失败通知书",
            "completed_chapters": 0,
            "total_chapters": 1,
            "output_path": "失败通知书.txt",
            "journal_path": "(hidden)",
            "latest_errors": [],
            "corrupt_lines": 0,
        }
        calls = []

        async def fake_wake(*_args):
            calls.append("llm")
            return False

        async def fake_direct(*_args):
            calls.append("direct")
            return False

        self.plugin._wake_llm_for_download_result = fake_wake
        self.plugin._send_direct_download_notification = fake_direct
        await self.plugin._notify_download_finished(
            "job-delivery-failed", "run-delivery-failed"
        )
        await self.plugin._notify_download_finished(
            "job-delivery-failed", "run-delivery-failed"
        )

        self.assertEqual(calls, ["llm", "direct"])
        notification = self.plugin._notification_statuses_for_jobs(
            {"job-delivery-failed"}
        )["job-delivery-failed"]
        self.assertEqual(notification["state"], "delivery_failed")
        self.assertIn("LLM wake", notification["failure_summary"])
        rendered = self.plugin.renderer.render_status(
            self.plugin._attach_notification_status(
                self.plugin.manager.get_status("job-delivery-failed")
            ),
            False,
        )
        self.assertIn("通知: delivery_failed", rendered)

    async def test_llm_wake_executes_astrbot_background_agent_flow(self):
        recorded = {}

        class ToolSet(object):
            def add_tool(self, tool):
                recorded["tool"] = tool

        class MainAgentBuildConfig(object):
            def __init__(self, **kwargs):
                recorded["build_config"] = kwargs

        class Conversation(object):
            history = "[]"

        async def get_session_conv(**kwargs):
            recorded["conversation_event"] = kwargs["event"]
            return Conversation()

        class Runner(object):
            async def step_until_done(self, _limit):
                await recorded["tool"].call(None)
                yield None

            def get_final_llm_resp(self):
                return types.SimpleNamespace(completion_text="已通知")

        async def build_main_agent(**kwargs):
            recorded["request"] = kwargs["req"]
            return types.SimpleNamespace(agent_runner=Runner())

        class CronMessageEvent(object):
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.role = ""

            def get_extra(self, key, default=None):
                return default

        class MessageSession(object):
            @staticmethod
            def from_str(value):
                recorded["session"] = value
                return ParsedSession(value)

        class ParsedSession(object):
            message_type = "FriendMessage"

            def __init__(self, value):
                self.value = value

            def __str__(self):
                return self.value

        class ProviderRequest(object):
            def __init__(self):
                self.conversation = None
                self.contexts = []
                self.system_prompt = ""
                self.prompt = ""
                self.func_tool = None

            def _print_friendly_context(self):
                return ""

        class SendMessageToUserTool(object):
            async def call(self, _context, **_kwargs):
                return "Message sent to session aiocqhttp:FriendMessage:42"

        async def persist_agent_history(*_args, **kwargs):
            recorded["history_event"] = kwargs["event"]

        fake_modules = {
            "astrbot.core.agent.tool": types.SimpleNamespace(ToolSet=ToolSet),
            "astrbot.core.astr_main_agent": types.SimpleNamespace(
                MainAgentBuildConfig=MainAgentBuildConfig,
                _get_session_conv=get_session_conv,
                build_main_agent=build_main_agent,
            ),
            "astrbot.core.astr_main_agent_resources": types.SimpleNamespace(
                BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT="result={background_task_result}"
            ),
            "astrbot.core.cron.events": types.SimpleNamespace(
                CronMessageEvent=CronMessageEvent
            ),
            "astrbot.core.platform.message_session": types.SimpleNamespace(
                MessageSession=MessageSession
            ),
            "astrbot.core.provider.entities": types.SimpleNamespace(
                ProviderRequest=ProviderRequest
            ),
            "astrbot.core.tools.message_tools": types.SimpleNamespace(
                SendMessageToUserTool=SendMessageToUserTool
            ),
            "astrbot.core.utils.history_saver": types.SimpleNamespace(
                persist_agent_history=persist_agent_history
            ),
        }
        sys.modules.update(fake_modules)
        self.plugin.context = types.SimpleNamespace(
            get_config=lambda _origin: {"provider_settings": {}},
            conversation_manager=object(),
        )

        delivered = await self.plugin._wake_llm_for_download_result(
            {
                "job_id": "job-real-wake",
                "run_id": "run-real-wake",
                "unified_msg_origin": "aiocqhttp:FriendMessage:42",
                "role": "member",
            },
            {
                "job_id": "job-real-wake",
                "state": "assembled",
                "book_name": "唤醒测试书",
                "completed_chapters": 1,
                "total_chapters": 1,
            },
            "下载完成",
        )

        self.assertTrue(delivered)
        self.assertIsInstance(recorded["tool"], SendMessageToUserTool)
        self.assertIn("send_message_to_user", recorded["request"].prompt)
        self.assertIn("session` argument empty", recorded["request"].prompt)
        self.assertIs(recorded["conversation_event"], recorded["history_event"])

    async def test_download_notification_retries_same_job_with_new_run_id(self):
        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:42",
            role="member",
            get_sender_id=lambda: "42",
        )
        statuses = iter(
            [
                {
                    "job_id": "job-retry",
                    "state": "failed",
                    "book_name": "重试通知测试书",
                    "completed_chapters": 0,
                    "total_chapters": 2,
                    "state_details": {"error": "temporary failure"},
                },
                {
                    "job_id": "job-retry",
                    "state": "assembled",
                    "book_name": "重试通知测试书",
                    "completed_chapters": 2,
                    "total_chapters": 2,
                    "output_filename": "重试通知测试书.txt",
                },
            ]
        )
        self.plugin.manager.get_status = lambda _job_id: next(statuses)
        sent = []

        async def fake_direct(callback, text):
            sent.append((callback["run_id"], text))
            return True

        self.plugin._send_direct_download_notification = fake_direct
        orchestration = {"job": {"job_id": "job-retry", "book_name": "重试通知测试书"}}
        self.plugin._register_download_callback(
            event, "job-retry", "attempt-failed", orchestration
        )
        await self.plugin._notify_download_finished("job-retry", "attempt-failed")
        self.plugin._register_download_callback(
            event, "job-retry", "attempt-success", orchestration
        )
        await self.plugin._notify_download_finished("job-retry", "attempt-success")

        self.assertEqual(
            [item[0] for item in sent], ["attempt-failed", "attempt-success"]
        )
        self.assertIn("下载失败", sent[0][1])
        self.assertIn("下载完成", sent[1][1])

    async def test_callback_storage_failure_does_not_block_task_start(self):
        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:42",
            role="member",
            get_sender_id=lambda: "42",
        )
        started = []

        def fail_write(_callbacks):
            raise OSError("read only callback storage")

        async def fake_run(job_id, auto_assemble, run_id):
            started.append((job_id, auto_assemble, run_id))

        self.plugin._write_download_callbacks_unlocked = fail_write
        self.plugin._run_rule_job = fake_run
        notification = await self.plugin._ensure_rule_job_running(
            "job-storage-failure",
            True,
            event=event,
            orchestration={"job": {"job_id": "job-storage-failure"}},
        )
        await self.plugin._running_tasks["job-storage-failure"]

        self.assertFalse(notification["enabled"])
        self.assertEqual(started[0][:2], ("job-storage-failure", True))
        self.assertEqual(started[0][2], notification["run_id"])

    async def test_cancelled_run_is_not_notified_and_resume_gets_new_run_id(self):
        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:42",
            role="member",
            get_sender_id=lambda: "42",
        )
        release_cancelled_worker = threading.Event()

        def blocked_resume(_job_id, _auto_assemble):
            release_cancelled_worker.wait(1.0)

        self.plugin.source_download_service.resume_book_job = blocked_resume
        first = await self.plugin._ensure_rule_job_running(
            "job-cancel-resume",
            True,
            event=event,
            orchestration={"job": {"job_id": "job-cancel-resume"}},
        )
        first_task = self.plugin._running_tasks["job-cancel-resume"]
        await asyncio.sleep(0)
        first_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_task
        release_cancelled_worker.set()

        sent = []
        self.plugin.source_download_service.resume_book_job = lambda *_args: None
        self.plugin.manager.get_status = lambda _job_id: {
            "job_id": "job-cancel-resume",
            "state": "downloaded",
            "book_name": "取消恢复测试书",
            "completed_chapters": 1,
            "total_chapters": 1,
        }

        async def fake_direct(callback, text):
            sent.append((callback["run_id"], text))
            return True

        self.plugin._send_direct_download_notification = fake_direct
        second = await self.plugin._ensure_rule_job_running(
            "job-cancel-resume",
            True,
            event=event,
            orchestration={"job": {"job_id": "job-cancel-resume"}},
        )
        await self.plugin._running_tasks["job-cancel-resume"]

        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual([item[0] for item in sent], [second["run_id"]])

    def test_render_auto_download_summary_includes_notification_status(self):
        payload = json.loads(
            self.plugin.renderer.render_auto_download_summary(
                {
                    "status": "started",
                    "notification": {
                        "enabled": True,
                        "mode": "direct",
                        "final_only": True,
                        "run_id": "run-summary",
                    },
                },
                {"search_id": "search-1", "path": "search.json"},
                {},
            )
        )

        self.assertEqual(payload["notification"]["run_id"], "run-summary")

    async def test_job_status_enforces_owner_and_redacts_paths_for_regular_user(self):
        job = self.plugin.manager.create_job(
            "权限测试书",
            [{"title": "第一章", "url": "https://example.com/chapter/1"}],
            self.module.ExtractionRules(content_regex=r"(?s)(.*)"),
            requester_id="owner-1",
            session_id="aiocqhttp:FriendMessage:owner-1",
        )
        owner_event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:owner-1",
            role="member",
            get_sender_id=lambda: "owner-1",
        )
        other_event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:owner-2",
            role="member",
            get_sender_id=lambda: "owner-2",
        )
        admin_event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:admin",
            role="admin",
            get_sender_id=lambda: "admin",
        )

        owner_status = await self.plugin.handle_novel_download_status(
            job["job_id"], event=owner_event
        )
        self.assertIn("输出: 权限测试书.txt", owner_status)
        self.assertNotIn(str(self.plugin.manager.output_dir), owner_status)
        self.assertNotIn(str(self.plugin.manager.jobs_dir), owner_status)
        with self.assertRaises(PermissionError):
            await self.plugin.handle_novel_download_status(
                job["job_id"], event=other_event
            )
        admin_status = await self.plugin.handle_novel_download_status(
            job["job_id"], event=admin_event
        )
        self.assertIn(str(self.plugin.manager.output_dir), admin_status)

        other_jobs = await self.plugin.handle_novel_download_status(event=other_event)
        self.assertEqual(other_jobs, "当前没有任何下载任务。")
        admin_jobs = json.loads(
            await self.plugin.handle_novel_list_jobs(event=admin_event)
        )
        self.assertEqual(admin_jobs["total_count"], 1)

    async def test_webnovel_refresh_sources_accepts_runtime_call_without_event_argument(
        self,
    ):
        recorded = {}

        async def fake_handle(source_ids_json="", include_disabled=""):
            recorded["source_ids_json"] = source_ids_json
            recorded["include_disabled"] = include_disabled
            return "ok"

        self.plugin.handle_novel_refresh_sources = fake_handle
        result = await self.plugin.webnovel_refresh_sources("a,b,c", "true")

        self.assertEqual(result, "ok")
        self.assertEqual(
            recorded,
            {
                "source_ids_json": "a,b,c",
                "include_disabled": "true",
            },
        )

    async def test_handle_novel_query_candidates_supports_offset_pagination(self):
        recorded = {}
        candidates = [
            {
                "candidate_index": 0,
                "source_id": "source-a",
                "source_name": "候选源A",
                "title": "分页测试书",
                "author": "作者甲",
                "book_url": "https://example.com/books/a",
                "supports_download": True,
                "search_health_state": "healthy",
                "preflight_health_state": "unknown",
                "download_health_state": "unknown",
            },
            {
                "candidate_index": 1,
                "source_id": "source-b",
                "source_name": "候选源B",
                "title": "分页测试书",
                "author": "作者甲",
                "book_url": "https://example.com/books/b",
                "supports_download": True,
                "search_health_state": "healthy",
                "preflight_health_state": "unknown",
                "download_health_state": "unknown",
            },
        ]

        class FakeResolver(object):
            def resolve(
                self,
                keyword,
                author="",
                source_ids=None,
                limit=20,
                include_disabled=False,
            ):
                recorded["resolve"] = {
                    "keyword": keyword,
                    "author": author,
                    "source_ids": source_ids,
                    "limit": limit,
                    "include_disabled": include_disabled,
                }
                return {
                    "keyword": keyword,
                    "author": author,
                    "search_result": {
                        "candidate_sources": 2,
                        "searched_sources": 2,
                        "successful_sources": 2,
                        "result_count": 2,
                        "errors": [],
                    },
                    "candidate_count": len(candidates),
                    "skipped_candidate_count": 0,
                    "candidates": candidates,
                    "skipped_candidates": [],
                    "candidate_groups": [
                        {
                            "group_index": 0,
                            "group_id": "book-a",
                            "title": "分页测试书",
                            "author": "作者甲",
                            "source_count": 2,
                            "candidate_count": 2,
                            "skipped_candidate_count": 0,
                            "downloadable_source_count": 2,
                            "best_source_name": "候选源A",
                            "candidates": candidates,
                            "skipped_candidates": [],
                        }
                    ],
                }

        original_resolver = self.plugin.book_resolution_service
        original_save_search = self.plugin.search_cache.save_search

        def fake_save_search(
            keyword,
            result,
            source_ids=None,
            include_disabled=False,
            limit=None,
        ):
            recorded["save_search"] = {
                "keyword": keyword,
                "source_ids": source_ids,
                "include_disabled": include_disabled,
                "limit": limit,
                "result": result,
            }
            return {"search_id": "search-1", "path": "search.json"}

        self.plugin.book_resolution_service = FakeResolver()
        self.plugin.search_cache.save_search = fake_save_search
        try:
            text = await self.plugin.handle_novel_query_candidates(
                "分页测试书",
                "作者甲",
                '["source-a"]',
                "1",
                "1",
                "true",
            )
        finally:
            self.plugin.book_resolution_service = original_resolver
            self.plugin.search_cache.save_search = original_save_search

        payload = json.loads(text)
        self.assertEqual(payload["candidate_count"], 2)
        self.assertEqual(payload["returned_candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["source_id"], "source-b")
        self.assertEqual(recorded["resolve"]["limit"], 2)
        self.assertEqual(recorded["resolve"]["source_ids"], ["source-a"])
        self.assertTrue(recorded["resolve"]["include_disabled"])

    async def test_webnovel_probe_status_returns_paginated_summary_without_event_argument(
        self,
    ):
        recorded = {}
        original_load = self.plugin.source_registry.load_enabled_source_summaries
        original_enrich = self.plugin.source_health_store.enrich_sources
        original_get_status = self.plugin.source_probe_service.get_status

        def fake_load_enabled_source_summaries(source_ids=None, include_disabled=False):
            recorded["load"] = {
                "source_ids": source_ids,
                "include_disabled": include_disabled,
            }
            return [
                {
                    "source_id": "source-1",
                    "name": "状态源一",
                    "enabled": True,
                    "supports_search": True,
                    "supports_download": True,
                    "search_health_state": "healthy",
                    "preflight_health_state": "healthy",
                    "download_health_state": "unknown",
                },
                {
                    "source_id": "source-2",
                    "name": "状态源二",
                    "enabled": True,
                    "supports_search": True,
                    "supports_download": True,
                    "search_health_state": "broken",
                    "preflight_health_state": "unknown",
                    "download_health_state": "unknown",
                },
                {
                    "source_id": "source-3",
                    "name": "状态源三",
                    "enabled": False,
                    "supports_search": False,
                    "supports_download": False,
                    "search_health_state": "unknown",
                    "preflight_health_state": "unsupported",
                    "download_health_state": "unsupported",
                },
            ]

        def fake_enrich_sources(sources):
            recorded["enrich_count"] = len(sources)
            return list(sources)

        def fake_get_status(preview_limit=0):
            recorded["status_preview_limit"] = preview_limit
            return {
                "workers_started": True,
                "queued_count": 2,
                "active_count": 1,
                "max_workers": 4,
                "queued_source_ids": ["source-3", "source-4"],
                "active_source_ids": ["source-2"],
                "omitted_queued_count": 0,
                "omitted_active_count": 0,
            }

        self.plugin.source_registry.load_enabled_source_summaries = (
            fake_load_enabled_source_summaries
        )
        self.plugin.source_health_store.enrich_sources = fake_enrich_sources
        self.plugin.source_probe_service.get_status = fake_get_status
        try:
            payload = json.loads(
                await self.plugin.webnovel_probe_status(
                    "source-1,source-2,source-3",
                    "true",
                    "1",
                    "1",
                )
            )
        finally:
            self.plugin.source_registry.load_enabled_source_summaries = original_load
            self.plugin.source_health_store.enrich_sources = original_enrich
            self.plugin.source_probe_service.get_status = original_get_status

        self.assertEqual(
            recorded["load"],
            {
                "source_ids": ["source-1", "source-2", "source-3"],
                "include_disabled": True,
            },
        )
        self.assertEqual(recorded["enrich_count"], 3)
        self.assertEqual(
            recorded["status_preview_limit"],
            self.plugin.max_tool_preview_items,
        )
        self.assertEqual(payload["requested_source_count"], 3)
        self.assertEqual(payload["selected_source_count"], 3)
        self.assertTrue(payload["include_disabled"])
        self.assertTrue(payload["workers_started"])
        self.assertEqual(payload["queued_probe_count"], 2)
        self.assertEqual(payload["active_probe_count"], 1)
        self.assertEqual(
            payload["search_health_counts"], {"healthy": 1, "broken": 1, "unknown": 1}
        )
        self.assertEqual(
            payload["preflight_health_counts"],
            {"healthy": 1, "unknown": 1, "unsupported": 1},
        )
        self.assertEqual(payload["offset"], 1)
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["returned_count"], 1)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_offset"], 2)
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["source_id"], "source-2")

    async def test_handle_novel_download_source_book_uses_search_candidate_url(self):
        recorded = {}

        class FakeBookResolutionService(object):
            def resolve(
                self,
                keyword,
                author,
                source_ids=None,
                limit=20,
                include_disabled=False,
            ):
                recorded["resolve"] = {
                    "keyword": keyword,
                    "author": author,
                    "source_ids": list(source_ids or []),
                    "limit": limit,
                    "include_disabled": include_disabled,
                }
                return {
                    "candidates": [
                        {
                            "source_id": "source-1",
                            "title": "测试书",
                            "author": "测试作者",
                            "book_url": "https://example.com/from-search",
                        }
                    ]
                }

        async def fake_download(
            source_id,
            book_url,
            book_name="",
            output_filename="",
            auto_assemble="true",
            expected_title="",
            expected_author="",
        ):
            recorded["download"] = {
                "source_id": source_id,
                "book_url": book_url,
                "book_name": book_name,
                "output_filename": output_filename,
                "auto_assemble": auto_assemble,
                "expected_title": expected_title,
                "expected_author": expected_author,
            }
            return "ok"

        self.plugin.book_resolution_service = FakeBookResolutionService()
        self.plugin.handle_novel_download_book = fake_download

        result = await self.plugin.handle_novel_download_source_book(
            "source-1",
            "测试书",
            "测试作者",
            "custom-name",
            "false",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(
            recorded["resolve"],
            {
                "keyword": "测试书",
                "author": "测试作者",
                "source_ids": ["source-1"],
                "limit": 20,
                "include_disabled": False,
            },
        )
        self.assertEqual(
            recorded["download"],
            {
                "source_id": "source-1",
                "book_url": "https://example.com/from-search",
                "book_name": "测试书",
                "output_filename": "custom-name",
                "auto_assemble": "false",
                "expected_title": "测试书",
                "expected_author": "测试作者",
            },
        )

    async def test_handle_novel_download_source_book_rejects_non_exact_candidate(self):
        class FakeBookResolutionService(object):
            def resolve(self, *_args, **_kwargs):
                return {
                    "candidates": [
                        {
                            "source_id": "source-1",
                            "title": "测试书",
                            "author": "另一个作者",
                            "book_url": "https://example.com/wrong-author",
                        }
                    ]
                }

        self.plugin.book_resolution_service = FakeBookResolutionService()
        with self.assertRaisesRegex(ValueError, "精确匹配"):
            await self.plugin.handle_novel_download_source_book(
                "source-1",
                "测试书",
                "测试作者",
            )

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

    def test_plugin_init_rejects_non_positive_max_response_bytes(self):
        with self.assertRaisesRegex(ValueError, "max_response_bytes.*必须大于 0"):
            self.module.JsonlNovelDownloaderPlugin(
                context=object(),
                config={"max_response_bytes": 0},
            )

    def test_open_url_ignores_env_proxy_by_default(self):
        http_utils = importlib.import_module(
            "astrbot_plugin_webnovel_downloader.http_utils"
        )
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

            def close(self):
                called["closed"] = True

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
        self.assertEqual(called["request_kwargs"]["method"], "GET")
        self.assertEqual(called["request_kwargs"]["timeout"], 12.0)
        self.assertFalse(called["client_kwargs"]["trust_env"])

    def test_open_url_can_use_env_proxy_when_enabled(self):
        http_utils = importlib.import_module(
            "astrbot_plugin_webnovel_downloader.http_utils"
        )
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

            def close(self):
                called["closed"] = True

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
        self.assertEqual(called["request_kwargs"]["timeout"], 8.5)
        self.assertTrue(called["client_kwargs"]["trust_env"])

    def test_open_url_reuses_httpx_client_per_proxy_mode(self):
        http_utils = importlib.import_module(
            "astrbot_plugin_webnovel_downloader.http_utils"
        )
        called = {
            "client_inits": 0,
            "request_timeouts": [],
        }

        class FakeResponse(object):
            status_code = 200
            reason_phrase = "OK"
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            content = b"reused-client"
            url = "https://example.com/reused"

        class FakeClient(object):
            def __init__(self, **kwargs):
                called["client_inits"] += 1
                called.setdefault("client_kwargs", []).append(kwargs)

            def close(self):
                called["closed"] = called.get("closed", 0) + 1

            def request(self, **kwargs):
                called["request_timeouts"].append(kwargs.get("timeout"))
                return FakeResponse()

        original_httpx = http_utils.httpx
        http_utils.httpx = types.SimpleNamespace(Client=FakeClient)
        try:
            first = http_utils.open_url(
                Request("https://example.com/a"),
                2.0,
                use_env_proxy=False,
            )
            second = http_utils.open_url(
                Request("https://example.com/b"),
                4.5,
                use_env_proxy=False,
            )
            third = http_utils.open_url(
                Request("https://example.com/c"),
                6.0,
                use_env_proxy=True,
            )
        finally:
            http_utils.httpx = original_httpx

        self.assertEqual(first.read(), b"reused-client")
        self.assertEqual(second.read(), b"reused-client")
        self.assertEqual(third.read(), b"reused-client")
        self.assertEqual(called["client_inits"], 2)
        self.assertEqual(called["request_timeouts"], [2.0, 4.5, 6.0])

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
                    "cleanRuleUrl": (self.base_dir / "clean_rules.txt")
                    .resolve()
                    .as_uri(),
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
                    },
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

        import_result = json.loads(
            await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        )
        self.assertEqual(import_result["imported_count"], 1)
        self.assertTrue(Path(import_result["registry_path"]).exists())
        self.assertEqual(import_result["source_count"], 1)

        listed_sources = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        self.assertEqual(listed_sources["total_count"], 1)
        self.assertEqual(listed_sources["sources"][0]["name"], "测试JSON源")

        search_result = json.loads(
            await self._invoke_tool(self.plugin.webnovel_search_books, "雪中")
        )
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
            await self.plugin.handle_novel_get_search_results(
                search_result["search_id"], "1", "0"
            )
        )
        self.assertEqual(cached_results["total_result_count"], 1)
        self.assertEqual(cached_results["results"][0]["result_index"], 0)

        preview = json.loads(
            await self.plugin.handle_novel_fetch_preview(
                (chapters_dir / "1.html").resolve().as_uri(),
                "",
                "200",
            )
        )
        self.assertIn("第一章 降生", preview["text_preview"])

        auto_download_payload = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_download_book,
                search_result["search_id"],
                "0",
                "",
                "",
                "true",
            )
        )
        self.assertEqual(auto_download_payload["status"], "started")
        auto_job_id = auto_download_payload["job"]["job_id"]
        auto_status = await self._invoke_tool(
            self.plugin.webnovel_download_status, auto_job_id
        )
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
        start_text = await self.plugin.handle_novel_start_download(
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

        status_text = await self.plugin.handle_webnovel_download_status(job_id)
        self.assertIn("状态: assembled", status_text)

        assembled_text = await self.plugin.handle_novel_assemble_book(job_id, "false")
        self.assertIn("状态: assembled", assembled_text)

    async def test_novel_download_end_to_end(self):
        chapters_dir = self.base_dir / "auto-good-chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "1.html").write_text(
            "<html><body><h1>第一章</h1><div id='content'>自动下载第一章</div></body></html>",
            encoding="utf-8",
        )
        (chapters_dir / "2.html").write_text(
            "<html><body><h1>第二章</h1><div id='content'>自动下载第二章</div></body></html>",
            encoding="utf-8",
        )
        (self.base_dir / "auto-good-book.html").write_text(
            "<html><body>"
            "<h1>自动下载测试书</h1>"
            "<div class='author'>自动作者</div>"
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
        (self.base_dir / "auto-bad-search.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "自动下载测试书",
                                "author": "自动作者",
                                "url": "https://example.com/bad-book",
                                "intro": "失败候选",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.base_dir / "auto-good-search.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "自动下载测试书",
                                "author": "自动作者",
                                "url": (self.base_dir / "auto-good-book.html")
                                .resolve()
                                .as_uri(),
                                "intro": "成功候选",
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
                    "bookSourceName": "A失败源",
                    "bookSourceUrl": "https://example.com/bad",
                    "searchUrl": (self.base_dir / "auto-bad-search.json")
                    .resolve()
                    .as_uri(),
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
                    "bookSourceName": "B成功源",
                    "bookSourceUrl": "https://example.com/good",
                    "searchUrl": (self.base_dir / "auto-good-search.json")
                    .resolve()
                    .as_uri(),
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
                },
            ],
            ensure_ascii=False,
        )

        self.plugin.auto_probe_on_import = False
        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        prepare_result = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_search_books,
                "自动下载测试书",
                "自动作者",
                "10",
                "false",
            )
        )
        self.assertGreaterEqual(prepare_result["candidate_count"], 2)
        self.assertTrue(Path(prepare_result["search_path"]).exists())

        download_payload = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_download_book,
                prepare_result["search_id"],
                "0",
                "2",
                "",
                "true",
            )
        )
        self.assertEqual(download_payload["status"], "started")
        self.assertEqual(download_payload["selected"]["source_name"], "B成功源")
        job_id = download_payload["job"]["job_id"]
        status_text = await self._invoke_tool(
            self.plugin.webnovel_download_status, job_id
        )
        self.assertIn("状态: assembled", status_text)
        output_path = self.plugin.manager.output_dir / "自动下载测试书.txt"
        self.assertTrue(output_path.exists())
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("自动下载第一章", content)
        self.assertIn("自动下载第二章", content)

    async def test_query_tools_return_source_detail_candidates_and_inspection(self):
        chapters_dir = self.base_dir / "query-chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "1.html").write_text(
            "<html><body><h1>第一章</h1><div id='content'>查询工具第一章正文</div></body></html>",
            encoding="utf-8",
        )
        book_path = self.base_dir / "query-book.html"
        book_path.write_text(
            "<html><body>"
            "<h1>查询测试书</h1>"
            "<div class='author'>查询作者</div>"
            "<div id='toc'><a href='{c1}'>第一章</a></div>"
            "</body></html>".format(c1=(chapters_dir / "1.html").resolve().as_uri()),
            encoding="utf-8",
        )
        search_path = self.base_dir / "query-search.json"
        search_path.write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "查询测试书",
                                "author": "查询作者",
                                "url": book_path.resolve().as_uri(),
                                "intro": "查询简介",
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
                    "bookSourceName": "查询源",
                    "bookSourceUrl": "https://example.com/query",
                    "searchUrl": search_path.resolve().as_uri(),
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

        self.plugin.auto_probe_on_import = False
        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        listed_sources = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        source_id = listed_sources["sources"][0]["source_id"]

        source_detail = json.loads(
            await self.plugin.handle_novel_get_source_detail(source_id)
        )
        self.assertEqual(source_detail["source"]["source_id"], source_id)
        self.assertEqual(source_detail["profile"]["template_family"], "generic_html")
        self.assertIn("rule_search_keys", source_detail["normalized"])

        candidate_query = json.loads(
            await self.plugin.handle_novel_query_candidates("查询测试书", "查询作者")
        )
        self.assertEqual(candidate_query["candidate_count"], 1)
        self.assertEqual(candidate_query["candidates"][0]["source_id"], source_id)
        self.assertTrue(candidate_query["search_id"])

        inspection = json.loads(
            await self.plugin.handle_novel_inspect_source_book(
                source_id,
                book_path.resolve().as_uri(),
                "查询测试书",
            )
        )
        self.assertEqual(inspection["status"], "ready")
        self.assertEqual(inspection["preflight"]["toc_count"], 1)
        self.assertEqual(inspection["sample"]["sampled_chapter_count"], 1)

    async def test_human_commands_smoke(self):
        (self.base_dir / "search-command.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "命令测试书",
                                "author": "命令作者",
                                "url": (self.base_dir / "cmd-book.html")
                                .resolve()
                                .as_uri(),
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
                    "searchUrl": (self.base_dir / "search-command.json")
                    .resolve()
                    .as_uri(),
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

        import_text = await self._invoke_command(
            self.plugin.novel_import_command, source_json
        )
        self.assertIn("imported_count", import_text)

        sources_text = await self._invoke_command(self.plugin.novel_sources_command)
        self.assertIn("命令测试源", sources_text)

        search_text = await self._invoke_command(
            self.plugin.novel_search_command, "命令测试书"
        )
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

        plugin = self._create_plugin(
            {
                "book_sources": [str(source_path)],
                "clean_rule_sources": [str(clean_path)],
            }
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

        plugin_base = importlib.import_module("astrbot_plugin_webnovel_downloader.base")
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
            plugin = self._create_plugin({"book_sources": [str(source_path)]})
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
                shutdown_probe = getattr(plugin.source_probe_service, "shutdown", None)
                if callable(shutdown_probe):
                    shutdown_probe(2.0)

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

        plugin = self._create_plugin({"book_sources": [str(source_path)]})
        self.assertTrue(plugin.wait_for_bootstrap(2.0))
        self.assertEqual(len(plugin.source_registry.list_sources()), 1)

        plugin_base = importlib.import_module("astrbot_plugin_webnovel_downloader.base")
        original_loader = plugin_base.load_text_argument

        def should_not_run(*args, **kwargs):
            raise AssertionError("重复配置导入不应再次请求 load_text_argument")

        plugin_base.load_text_argument = should_not_run
        try:
            plugin_again = self._create_plugin({"book_sources": [str(source_path)]})
            self.assertTrue(plugin_again.wait_for_bootstrap(0.1))
            self.assertEqual(len(plugin_again.source_registry.list_sources()), 1)
        finally:
            plugin_base.load_text_argument = original_loader

    def test_install_bundled_skill_uses_skill_manager_and_syncs_sandbox(self):
        importlib.import_module("astrbot_plugin_webnovel_downloader.base")
        skill_dir = self.plugin_dir / "skills" / "webnovel-downloader-workflow"

        astrbot_core_skills = types.ModuleType("astrbot.core.skills")
        astrbot_core_skill_manager = types.ModuleType(
            "astrbot.core.skills.skill_manager"
        )
        astrbot_core_computer = types.ModuleType("astrbot.core.computer")
        astrbot_core_computer_client = types.ModuleType(
            "astrbot.core.computer.computer_client"
        )

        recorded: dict[str, object] = {}
        synced_calls: list[bool] = []

        class FakeSkillManager(object):
            def __init__(self, skills_root=None):
                recorded["skills_root"] = skills_root

            def list_skills(self):
                return []

            def install_skill_from_zip(
                self,
                zip_path,
                *,
                overwrite=True,
                skill_name_hint=None,
            ):
                recorded["zip_path"] = zip_path
                recorded["overwrite"] = overwrite
                recorded["skill_name_hint"] = skill_name_hint
                with zipfile.ZipFile(zip_path) as archive:
                    names = set(archive.namelist())
                self_outer = self
                _ = self_outer
                assert "webnovel-downloader-workflow/SKILL.md" in names
                return "webnovel-downloader-workflow"

        async def fake_sync_skills_to_active_sandboxes():
            synced_calls.append(True)

        astrbot_core_skill_manager.SkillManager = FakeSkillManager
        astrbot_core_computer_client.sync_skills_to_active_sandboxes = (
            fake_sync_skills_to_active_sandboxes
        )

        sys.modules["astrbot.core.skills"] = astrbot_core_skills
        sys.modules["astrbot.core.skills.skill_manager"] = astrbot_core_skill_manager
        sys.modules["astrbot.core.computer"] = astrbot_core_computer
        sys.modules["astrbot.core.computer.computer_client"] = (
            astrbot_core_computer_client
        )

        result = self.plugin._install_bundled_skill(skill_dir, overwrite=True)
        self.assertEqual(recorded["skill_name_hint"], "webnovel-downloader-workflow")
        self.assertTrue(recorded["overwrite"])
        self.assertTrue(result["overwrite"])
        self.assertEqual(result["installed_name"], "webnovel-downloader-workflow")
        self.assertTrue(result["synced_sandboxes"])
        self.assertEqual(synced_calls, [True])

    def test_bundled_skill_update_overwrites_managed_and_migrates_legacy_state(self):
        managed_dir = self.base_dir / "managed-skill"
        legacy_dir = self.base_dir / "legacy-skill"
        external_dir = self.base_dir / "external-skill"
        for skill_dir in (managed_dir, legacy_dir, external_dir):
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: {name}\nversion: 2.0.0\n---\n".format(name=skill_dir.name),
                encoding="utf-8",
            )

        self.plugin._get_installed_skill_names = lambda: {
            managed_dir.name,
            legacy_dir.name,
            external_dir.name,
        }
        for skill_dir, install_action in (
            (managed_dir, "installed"),
            (legacy_dir, "already_exists"),
        ):
            ref = str(skill_dir)
            self.plugin._save_bootstrap_result(
                "bundled_skills",
                self.plugin._build_bootstrap_entry_id(ref),
                ref,
                "old-signature",
                "success",
                time.time(),
                skill_name=skill_dir.name,
                skill_version="1.0.0",
                install_action=install_action,
            )

        pending = self.plugin._filter_bootstrap_skill_dirs(
            [managed_dir, legacy_dir, external_dir]
        )

        self.assertEqual(pending, [managed_dir, legacy_dir])
        state = json.loads(self.plugin._bootstrap_state_path.read_text("utf-8"))
        external_entry = state["bundled_skills"][
            self.plugin._build_bootstrap_entry_id(str(external_dir))
        ]
        self.assertEqual(external_entry["install_action"], "already_exists")
        self.assertFalse(external_entry["managed_by_plugin"])

    def test_plugin_bootstrap_auto_installs_bundled_skills_in_background(self):
        plugin_base = importlib.import_module("astrbot_plugin_webnovel_downloader.base")
        demo_skill_dir = self.base_dir / "demo-skill"
        demo_skill_dir.mkdir(parents=True, exist_ok=True)
        (demo_skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\n",
            encoding="utf-8",
        )

        original_list = (
            plugin_base.JsonlNovelDownloaderPluginBase._list_bundled_skill_dirs
        )
        original_get_names = (
            plugin_base.JsonlNovelDownloaderPluginBase._get_installed_skill_names
        )
        original_install = (
            plugin_base.JsonlNovelDownloaderPluginBase._install_bundled_skill
        )
        started = threading.Event()
        unblock = threading.Event()
        installed: list[tuple[str, bool]] = []

        def fake_list(self):
            return [demo_skill_dir]

        def fake_get_names(self):
            return set()

        def slow_install(self, skill_dir, *, overwrite=False):
            started.set()
            unblock.wait(1.0)
            installed.append((skill_dir.name, overwrite))
            return {
                "installed_name": skill_dir.name,
                "synced_sandboxes": False,
            }

        plugin_base.JsonlNovelDownloaderPluginBase._list_bundled_skill_dirs = fake_list
        plugin_base.JsonlNovelDownloaderPluginBase._get_installed_skill_names = (
            fake_get_names
        )
        plugin_base.JsonlNovelDownloaderPluginBase._install_bundled_skill = slow_install
        plugin = None
        try:
            begin = time.perf_counter()
            plugin = self._create_plugin({})
            elapsed = time.perf_counter() - begin
            self.assertLess(elapsed, 0.2)
            self.assertTrue(started.wait(1.0))
            self.assertEqual(installed, [])

            unblock.set()
            self.assertTrue(plugin.wait_for_bootstrap(2.0))
            self.assertEqual(installed, [("demo-skill", False)])
        finally:
            unblock.set()
            plugin_base.JsonlNovelDownloaderPluginBase._list_bundled_skill_dirs = (
                original_list
            )
            plugin_base.JsonlNovelDownloaderPluginBase._get_installed_skill_names = (
                original_get_names
            )
            plugin_base.JsonlNovelDownloaderPluginBase._install_bundled_skill = (
                original_install
            )
            if plugin is not None:
                plugin.wait_for_bootstrap(2.0)

    def test_plugin_bootstrap_can_disable_bundled_skill_auto_install(self):
        plugin_base = importlib.import_module("astrbot_plugin_webnovel_downloader.base")
        demo_skill_dir = self.base_dir / "disabled-skill"
        demo_skill_dir.mkdir(parents=True, exist_ok=True)
        (demo_skill_dir / "SKILL.md").write_text(
            "---\nname: disabled-skill\nversion: 9.8.7\n---\n",
            encoding="utf-8",
        )

        original_list = (
            plugin_base.JsonlNovelDownloaderPluginBase._list_bundled_skill_dirs
        )
        original_install = (
            plugin_base.JsonlNovelDownloaderPluginBase._install_bundled_skill
        )
        installed: list[str] = []

        def fake_list(self):
            return [demo_skill_dir]

        def fail_if_called(self, skill_dir, *, overwrite=False):
            del overwrite
            installed.append(skill_dir.name)
            raise AssertionError("disabled bundled skill install was called")

        plugin_base.JsonlNovelDownloaderPluginBase._list_bundled_skill_dirs = fake_list
        plugin_base.JsonlNovelDownloaderPluginBase._install_bundled_skill = (
            fail_if_called
        )
        plugin = None
        try:
            plugin = self._create_plugin({"auto_install_bundled_skills": False})
            self.assertTrue(plugin.wait_for_bootstrap(0.1))
            self.assertEqual(installed, [])

            state = json.loads(plugin._bootstrap_state_path.read_text("utf-8"))
            entry = next(iter(state["bundled_skills"].values()))
            self.assertEqual(entry["status"], "disabled")
            self.assertEqual(entry["install_action"], "disabled_by_config")
            self.assertEqual(entry["skill_name"], "disabled-skill")
            self.assertEqual(entry["skill_version"], "9.8.7")
        finally:
            plugin_base.JsonlNovelDownloaderPluginBase._list_bundled_skill_dirs = (
                original_list
            )
            plugin_base.JsonlNovelDownloaderPluginBase._install_bundled_skill = (
                original_install
            )
            if plugin is not None:
                plugin.wait_for_bootstrap(2.0)

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
            task = asyncio.create_task(
                self.plugin.handle_webnovel_download_status("job-1")
            )
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
            task = asyncio.create_task(
                self.plugin.handle_webnovel_import_sources(source_json)
            )
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
            def load_enabled_source_summaries(
                self, source_ids=None, include_disabled=False
            ):
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

    def test_search_service_prioritizes_healthy_sources_and_stops_after_exact_matches(
        self,
    ):
        search_module = importlib.import_module(
            "astrbot_plugin_webnovel_downloader.core.search_service"
        )
        health_path = self.base_dir / "search-health.json"
        health_path.write_text(
            json.dumps(
                {
                    "sources": {
                        "fast": {
                            "attempts": 8,
                            "successes": 8,
                            "failures": 0,
                            "timeouts": 0,
                            "avg_duration_ms": 25.0,
                            "avg_success_ms": 25.0,
                            "last_success_at": 200.0,
                            "last_failure_at": 0.0,
                        },
                        "slow": {
                            "attempts": 5,
                            "successes": 1,
                            "failures": 4,
                            "timeouts": 2,
                            "avg_duration_ms": 1800.0,
                            "avg_success_ms": 1500.0,
                            "last_success_at": 100.0,
                            "last_failure_at": 190.0,
                        },
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        search_order: list[str] = []

        class FakeRegistry(object):
            def load_enabled_source_summaries(
                self, source_ids=None, include_disabled=False
            ):
                return [
                    {
                        "source_id": "slow",
                        "name": "慢源",
                        "supports_search": True,
                    },
                    {
                        "source_id": "fast",
                        "name": "快源",
                        "supports_search": True,
                    },
                    {
                        "source_id": "unknown",
                        "name": "未知源",
                        "supports_search": True,
                    },
                ]

            def load_enabled_sources(self, source_ids=None, include_disabled=False):
                return [
                    {"source_id": "slow", "name": "慢源"},
                    {"source_id": "fast", "name": "快源"},
                    {"source_id": "unknown", "name": "未知源"},
                ]

        class FakeEngine(object):
            def search_books(self, source, keyword, limit):
                search_order.append(source["source_id"])
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
                raise AssertionError("提前收手后不应继续搜索其他书源")

        service = search_module.SearchService(
            FakeRegistry(),
            FakeEngine(),
            search_module.SearchServiceConfig(
                max_workers=1,
                time_budget_seconds=10.0,
                health_path=health_path,
            ),
        )

        result = service.search("诡秘之主", limit=1)

        self.assertEqual(search_order, ["fast"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["early_stopped"])
        self.assertEqual(result["stop_reason"], "exact_match_limit")
        self.assertEqual(result["candidate_sources"], 3)
        self.assertEqual(result["searched_sources"], 1)
        self.assertEqual(result["unsearched_source_count"], 2)
        self.assertEqual(result["result_count"], 1)

    def test_plugin_runtime_supports_separate_search_timeout_and_workers(self):
        runtime_module = importlib.import_module(
            "astrbot_plugin_webnovel_downloader.runtime"
        )
        runtime = runtime_module.build_plugin_runtime(
            self.base_dir / "runtime",
            {
                "max_workers": 4,
                "search_max_workers": 9,
                "request_timeout": 20.0,
                "search_request_timeout": 5.0,
                "search_time_budget": 12.0,
            },
        )

        self.assertEqual(runtime.manager.config.max_workers, 4)
        self.assertEqual(runtime.search_service.config.max_workers, 9)
        self.assertEqual(runtime.search_service.engine.config.request_timeout, 5.0)
        self.assertEqual(
            runtime.source_download_service.engine.config.request_timeout, 20.0
        )
        self.assertTrue(
            str(runtime.search_service.config.health_path).endswith(
                "search_source_health.json"
            )
        )
        self.assertTrue(
            str(runtime.source_health_store.path).endswith("source_health.json")
        )
        self.assertEqual(runtime.source_probe_service.config.max_workers, 6)
        self.assertEqual(
            runtime.source_probe_service.config.probe_keywords,
            ("诡秘之主", "斗破苍穹", "凡人修仙传"),
        )

    def test_plugin_runtime_uses_fast_default_workers_and_timeouts(self):
        runtime_module = importlib.import_module(
            "astrbot_plugin_webnovel_downloader.runtime"
        )
        runtime = runtime_module.build_plugin_runtime(
            self.base_dir / "runtime-defaults"
        )

        self.assertEqual(runtime.manager.config.max_workers, 12)
        self.assertEqual(runtime.source_download_service.config.max_workers, 12)
        self.assertEqual(runtime.search_service.config.max_workers, 24)
        self.assertEqual(runtime.source_probe_service.config.max_workers, 6)
        self.assertEqual(runtime.manager.config.request_timeout, 15.0)
        self.assertEqual(
            runtime.source_download_service.engine.config.request_timeout, 15.0
        )
        self.assertEqual(runtime.search_service.engine.config.request_timeout, 8.0)
        self.assertEqual(runtime.search_service.config.time_budget_seconds, 60.0)

    async def test_import_sources_queues_probe_and_list_sources_shows_health(self):
        self.plugin.auto_probe_on_import = True
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "排队探测测试源",
                    "bookSourceUrl": "https://example.com/probe",
                    "searchUrl": "https://example.com/search?q={{key}}",
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "author": "author",
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
        queued_source_ids: list[str] = []
        original_enqueue_sources = self.plugin.source_probe_service.enqueue_sources
        try:

            def fake_enqueue_sources(source_ids):
                queued_source_ids.extend(list(source_ids))
                return {
                    "queued_count": len(list(source_ids)),
                    "queue_size": 7,
                }

            self.plugin.source_probe_service.enqueue_sources = fake_enqueue_sources
            imported = json.loads(
                await self.plugin.handle_webnovel_import_sources(source_json)
            )
        finally:
            self.plugin.source_probe_service.enqueue_sources = original_enqueue_sources

        self.assertEqual(imported["queued_probe_count"], 1)
        self.assertEqual(imported["probe_queue_size"], 7)
        self.assertEqual(len(queued_source_ids), 1)

        source_id = queued_source_ids[0]
        self.plugin.source_health_store.record_success(
            source_id,
            "search",
            summary="搜索探测成功",
        )
        self.plugin.source_health_store.mark_unknown(
            source_id,
            "preflight",
            summary="等待目录预检",
        )
        self.plugin.source_health_store.mark_unknown(
            source_id,
            "download",
            summary="尚未进行正文下载探测",
        )

        listed_sources = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        self.assertEqual(listed_sources["sources"][0]["source_id"], source_id)
        self.assertEqual(listed_sources["sources"][0]["search_health_state"], "healthy")
        self.assertEqual(
            listed_sources["sources"][0]["search_health_summary"], "搜索探测成功"
        )
        self.assertEqual(
            listed_sources["sources"][0]["preflight_health_state"], "unknown"
        )
        self.assertEqual(
            listed_sources["sources"][0]["download_health_state"], "unknown"
        )

    async def test_import_sources_can_disable_auto_probe(self):
        plugin = self._create_plugin(config={"auto_probe_on_import": False})
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "禁用自动探测源",
                    "bookSourceUrl": "https://example.com/no-probe",
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
        queued_source_ids: list[str] = []
        original_enqueue_sources = plugin.source_probe_service.enqueue_sources
        try:

            def fake_enqueue_sources(source_ids):
                queued_source_ids.extend(list(source_ids))
                return {
                    "queued_count": len(list(source_ids)),
                    "queue_size": 5,
                }

            plugin.source_probe_service.enqueue_sources = fake_enqueue_sources
            imported = json.loads(
                await plugin.handle_webnovel_import_sources(source_json)
            )
        finally:
            plugin.source_probe_service.enqueue_sources = original_enqueue_sources

        self.assertEqual(imported["queued_probe_count"], 0)
        self.assertEqual(imported["probe_queue_size"], 0)
        self.assertEqual(queued_source_ids, [])

    async def test_refresh_sources_can_queue_selected_sources(self):
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "刷新源A",
                    "bookSourceUrl": "https://example.com/a",
                    "searchUrl": "https://example.com/search?q={{key}}&a=1",
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
                },
                {
                    "bookSourceName": "刷新源B",
                    "bookSourceUrl": "https://example.com/b",
                    "searchUrl": "https://example.com/search?q={{key}}&b=1",
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
                },
            ],
            ensure_ascii=False,
        )
        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        listed = json.loads(await self._invoke_tool(self.plugin.webnovel_list_sources))
        source_ids = [item["source_id"] for item in listed["sources"]]
        queued_source_ids = []
        original_enqueue_sources = self.plugin.source_probe_service.enqueue_sources
        original_get_status = self.plugin.source_probe_service.get_status
        try:

            def fake_enqueue_sources(source_ids_to_queue):
                queued_source_ids.extend(source_ids_to_queue)
                return {
                    "queued_count": len(list(source_ids_to_queue)),
                    "queue_size": 6,
                }

            def fake_get_status():
                return {
                    "workers_started": True,
                    "queued_count": 6,
                    "active_count": 1,
                    "max_workers": 2,
                }

            self.plugin.source_probe_service.enqueue_sources = fake_enqueue_sources
            self.plugin.source_probe_service.get_status = fake_get_status
            result = json.loads(
                await self._invoke_tool(
                    self.plugin.webnovel_refresh_sources,
                    source_ids[0],
                    "false",
                )
            )
        finally:
            self.plugin.source_probe_service.enqueue_sources = original_enqueue_sources
            self.plugin.source_probe_service.get_status = original_get_status

        self.assertEqual(result["requested_source_count"], 1)
        self.assertEqual(result["selected_source_count"], 1)
        self.assertEqual(result["queued_probe_count"], 1)
        self.assertEqual(result["probe_queue_size"], 6)
        self.assertEqual(result["active_probe_count"], 1)
        self.assertEqual(queued_source_ids, [source_ids[0]])

    def test_plugin_init_rejects_non_positive_search_request_timeout(self):
        with self.assertRaisesRegex(ValueError, "search_request_timeout.*必须大于 0"):
            self.module.JsonlNovelDownloaderPlugin(
                context=object(),
                config={"search_request_timeout": 0},
            )

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
                                "url": (self.base_dir / "cache-book.html")
                                .resolve()
                                .as_uri(),
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
                    "searchUrl": (self.base_dir / "cache-search.json")
                    .resolve()
                    .as_uri(),
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        search_result = json.loads(
            await self._invoke_tool(self.plugin.webnovel_search_books, "缓存小说")
        )
        search_id = search_result["search_id"]
        self.assertTrue(search_id)
        self.assertEqual(search_result["results"][0]["result_index"], 0)

        searches = json.loads(await self.plugin.handle_novel_list_searches("10", "0"))
        self.assertEqual(searches["total_count"], 1)
        self.assertEqual(searches["searches"][0]["search_id"], search_id)

        cached_page = json.loads(
            await self.plugin.handle_novel_get_search_results(search_id, "10", "0")
        )
        self.assertEqual(cached_page["total_result_count"], 1)
        self.assertEqual(cached_page["results"][0]["title"], "缓存小说")

        download_payload = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_download_book,
                search_id,
                "0",
                "",
                "",
                "true",
            )
        )
        self.assertEqual(download_payload["status"], "started")
        job_id = download_payload["job"]["job_id"]
        status_text = await self._invoke_tool(
            self.plugin.webnovel_download_status, job_id
        )
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
                                "url": (self.base_dir / "clean-repo-book.html")
                                .resolve()
                                .as_uri(),
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
                    "searchUrl": (self.base_dir / "clean-repo-search.json")
                    .resolve()
                    .as_uri(),
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        import_result = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_import_clean_rules,
                clean_repo_json,
                "测试净化仓库",
            )
        )
        self.assertEqual(import_result["name"], "测试净化仓库")
        self.assertEqual(import_result["rule_count"], 1)
        self.assertEqual(import_result["scoped_rule_count"], 1)
        self.assertTrue(Path(import_result["path"]).exists())

        repo_list = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_clean_rules, "10", "0")
        )
        self.assertEqual(repo_list["total_count"], 1)
        self.assertEqual(repo_list["repositories"][0]["name"], "测试净化仓库")

        search_result = json.loads(
            await self._invoke_tool(self.plugin.webnovel_search_books, "净化仓库测试书")
        )
        download_payload = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_download_book,
                search_result["search_id"],
                "0",
                "",
                "",
                "true",
            )
        )
        self.assertEqual(download_payload["status"], "started")
        job_id = download_payload["job"]["job_id"]
        status_text = await self._invoke_tool(
            self.plugin.webnovel_download_status, job_id
        )
        self.assertIn("状态: assembled", status_text)
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
                self.plugin.webnovel_import_clean_rules,
                clean_repo_json,
                "跳过测试仓库",
            )
        )
        self.assertEqual(result["rule_count"], 1)
        self.assertEqual(result["skipped_rule_count"], 2)

        repo_list = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_clean_rules, "10", "0")
        )
        self.assertEqual(repo_list["repositories"][0]["rule_count"], 1)
        self.assertEqual(repo_list["repositories"][0]["skipped_rule_count"], 2)

    async def test_import_clean_rules_does_not_echo_large_inline_payload(self):
        clean_repo_json = json.dumps(
            [
                {
                    "name": "大仓库规则{index}".format(index=index),
                    "pattern": "广告{index}".format(index=index),
                    "replacement": "",
                    "isRegex": False,
                    "scopeContent": True,
                }
                for index in range(80)
            ],
            ensure_ascii=False,
        )

        text = await self._invoke_tool(
            self.plugin.webnovel_import_clean_rules,
            clean_repo_json,
            "大仓库",
        )
        result = json.loads(text)

        self.assertEqual(result["rule_count"], 80)
        self.assertEqual(result["source_ref_length"], len(clean_repo_json))
        self.assertTrue(result["source_ref_truncated"])
        self.assertLessEqual(
            len(result["source_ref"]),
            self.plugin.max_tool_preview_text,
        )
        self.assertLessEqual(len(text), self.plugin.max_tool_response_chars)

    async def test_plugin_terminate_shuts_down_probe_service(self):
        calls = []
        original_shutdown = self.plugin.source_probe_service.shutdown

        def fake_shutdown(timeout=None):
            calls.append(timeout)
            return True

        self.plugin.source_probe_service.shutdown = fake_shutdown
        try:
            await self.plugin.terminate()
        finally:
            self.plugin.source_probe_service.shutdown = original_shutdown

        self.assertEqual(calls, [5.0])

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

        result = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_import_sources, rss_like_source
            )
        )
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["supported_search_count"], 0)
        self.assertEqual(result["supported_download_count"], 0)
        self.assertGreater(result["warning_count"], 0)
        self.assertTrue(result["warnings_preview"])
        listed_sources = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        source = listed_sources["sources"][0]
        self.assertEqual(source["name"], "源仓库(官方纯净)")
        self.assertFalse(source["supports_search"])
        self.assertFalse(source["supports_download"])
        self.assertTrue(source["issues"])

    async def test_js_heavy_source_marks_login_runtime_unsupported_and_skips_search(
        self,
    ):
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

        result = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_import_sources, js_heavy_source
            )
        )
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["supported_search_count"], 0)
        self.assertEqual(result["supported_download_count"], 0)
        listed_sources = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        source = listed_sources["sources"][0]
        self.assertTrue(source["has_js_lib"])
        self.assertTrue(source["has_login_flow"])
        self.assertTrue(source["search_uses_js"])
        self.assertTrue(source["download_uses_js"])
        self.assertFalse(source["supports_search"])
        self.assertFalse(source["supports_download"])
        self.assertTrue(any("登录" in issue for issue in source["issues"]))

        search_result = json.loads(
            await self._invoke_tool(self.plugin.webnovel_search_books, "雪中")
        )
        self.assertEqual(search_result["searched_sources"], 0)
        self.assertEqual(len(search_result["skipped_sources"]), 1)
        self.assertIn("登录", search_result["skipped_sources"][0]["reason"])

    async def test_download_book_rejects_network_js_download_source_before_fetch(self):
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
                        "chapterList": "<js>java.ajax(baseUrl)</js>",
                    },
                    "ruleContent": {
                        "content": "div.content&&text",
                    },
                }
            ],
            ensure_ascii=False,
        )

        json.loads(
            await self._invoke_tool(self.plugin.webnovel_import_sources, partial_source)
        )
        listed_sources = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        source_id = listed_sources["sources"][0]["source_id"]
        self.assertTrue(listed_sources["sources"][0]["supports_search"])
        self.assertFalse(listed_sources["sources"][0]["supports_download"])

        with self.assertRaisesRegex(ValueError, "不支持 TXT 下载"):
            await self.plugin.handle_novel_download_book(
                source_id,
                "https://example.com/book/1",
                "测试书",
                "",
                "true",
            )
        listed_again = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        self.assertEqual(
            listed_again["sources"][0]["preflight_health_state"], "unsupported"
        )

    async def test_download_book_records_preflight_failure_before_starting_task(self):
        self.plugin.auto_probe_on_import = False
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "预检失败源",
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
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {
                        "content": "div.content&&text",
                    },
                }
            ],
            ensure_ascii=False,
        )
        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        listed_sources = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        source_id = listed_sources["sources"][0]["source_id"]
        original_preflight = self.plugin.source_download_service.preflight_book
        try:

            def fake_preflight(*args, **kwargs):
                raise ValueError("未解析到目录，请检查 ruleToc")

            self.plugin.source_download_service.preflight_book = fake_preflight
            with self.assertRaisesRegex(ValueError, "未解析到目录"):
                await self.plugin.handle_novel_download_book(
                    source_id,
                    "https://example.com/book/1",
                    "测试书",
                    "",
                    "true",
                )
        finally:
            self.plugin.source_download_service.preflight_book = original_preflight

        self.assertEqual(self.plugin._running_tasks, {})
        listed_again = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources)
        )
        self.assertEqual(
            listed_again["sources"][0]["preflight_health_state"], "degraded"
        )
        self.assertIn(
            "未解析到目录", listed_again["sources"][0]["preflight_health_summary"]
        )

    async def test_bulk_import_returns_compact_summary_with_local_registry(self):
        sources = [
            {
                "bookSourceName": "测试源{index}".format(index=index),
                "bookSourceUrl": "https://example.com/{index}".format(index=index),
                "searchUrl": "https://example.com/search?q={{key}}&source={index}".format(
                    index=index
                ),
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
                self.plugin.webnovel_import_sources,
                json.dumps(sources, ensure_ascii=False),
            )
        )
        self.assertEqual(result["imported_count"], 12)
        self.assertEqual(result["source_count"], 12)
        self.assertLessEqual(
            len(result["sources_preview"]), self.plugin.max_tool_preview_items
        )
        self.assertGreater(result["remaining_source_count"], 0)
        self.assertTrue(Path(result["registry_path"]).exists())
        self.assertNotIn("sources", result)

        second_page = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources, "", "4", "8")
        )
        self.assertEqual(second_page["returned_count"], 4)
        self.assertFalse(second_page["has_more"])

        compact_page = json.loads(
            await self._invoke_tool(self.plugin.webnovel_list_sources, "", "12", "0")
        )
        self.assertEqual(compact_page["returned_count"], 12)
        self.assertLessEqual(
            len(compact_page["sources"]), self.plugin.max_tool_preview_items
        )
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
                    "searchUrl": (self.base_dir / "search-many.json")
                    .resolve()
                    .as_uri(),
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        result = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_search_books, "测试", "", "12", "false"
            )
        )
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
                    "searchUrl": '/search-gbk?key={{key}}&page={{page}},{"charset":"gbk"}',
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
                    "searchUrl": '/search-post,{"method":"POST","charset":"gbk","body":"searchkey={{key}}&searchtype=all"}',
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
        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)

        result = json.loads(
            await self._invoke_tool(
                self.plugin.webnovel_search_books, "诡秘之主", "", "10", "false"
            )
        )
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
                    "searchUrl": (self.base_dir / "search-template.json")
                    .resolve()
                    .as_uri(),
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        result = json.loads(
            await self._invoke_tool(self.plugin.webnovel_search_books, "诡秘之主")
        )
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
                    "searchUrl": (self.base_dir / "search-chain.html")
                    .resolve()
                    .as_uri(),
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        result = json.loads(
            await self._invoke_tool(self.plugin.webnovel_search_books, "诡秘之主")
        )
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["successful_sources"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["results"][0]["title"], "诡秘之主")
        self.assertIn("爱潜水的乌贼", result["results"][0]["author"])
        self.assertEqual(result["results"][0]["kind"], "玄幻")
        self.assertEqual(
            result["results"][0]["book_url"], "https://example.com/books/1"
        )

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
                        "intro": '更新时间：{{@@[property="og:novel:update_time"]@content##-##/}}',
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(
            sources[0]["source_id"]
        )
        plan = self.plugin.search_service.engine.build_book_download_plan(
            source,
            detail_path.resolve().as_uri(),
            "",
        )

        self.assertEqual(plan["book_name"], "兼容测试书")
        self.assertEqual(plan["author"], "测试作者")
        self.assertIn("2026/04/17", plan["intro"])
        self.assertEqual(plan["toc"][0]["title"], "第一章")

    async def test_fetch_chapter_content_supports_text_paging_multi_node_and_replace_regex(
        self,
    ):
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(
            sources[0]["source_id"]
        )
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

    async def test_fetch_chapter_content_removes_generic_page_markers_without_replace_regex(
        self,
    ):
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(
            sources[0]["source_id"]
        )
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
            chapter_path = self.base_dir / "toc-many-chapter-{index}.html".format(
                index=index
            )
            chapter_path.write_text(
                "<html><body><div id='content'>正文{index}</div></body></html>".format(
                    index=index
                ),
                encoding="utf-8",
            )
            chapter_pages.append(chapter_path)

        for page_no in range(1, 8):
            toc_path = self.base_dir / "toc-many-page-{page}.html".format(page=page_no)
            next_link = ""
            if page_no < 7:
                next_link = "<a href='{href}'>下一页</a>".format(
                    href=(
                        self.base_dir
                        / "toc-many-page-{page}.html".format(page=page_no + 1)
                    )
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(
            sources[0]["source_id"]
        )
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(
            sources[0]["source_id"]
        )
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

    async def test_fetch_chapter_content_formats_chinese_paragraphs_and_merges_broken_page_lines(
        self,
    ):
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

        await self._invoke_tool(self.plugin.webnovel_import_sources, source_json)
        sources = self.plugin.source_registry.list_sources()
        source = self.plugin.source_registry.load_normalized_source(
            sources[0]["source_id"]
        )
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

    async def test_fetch_chapter_content_preserves_text_rule_block_paragraphs(self):
        chapter_page = self.base_dir / "chapter-text-rule-paragraphs.html"
        chapter_page.write_text(
            "<html><body>"
            "<h1>第一章</h1>"
            "<div id='content'>"
            "<p>第一段应该保留换行。</p>"
            "<p>第二段也应该另起一行。</p>"
            "</div>"
            "</body></html>",
            encoding="utf-8",
        )
        source = {
            "source_id": "paragraph-source",
            "source_url": "https://example.com",
            "headers": {},
            "rule_content": {
                "title": "h1&&text",
                "content": "#content&&text",
            },
        }

        chapter = (
            self.plugin.search_service.engine.fallback_extractor.fetch_chapter_content(
                source,
                chapter_page.resolve().as_uri(),
                "第一章",
            )
        )

        lines = [line for line in chapter["content"].splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].strip(), "第一段应该保留换行。")
        self.assertEqual(lines[1].strip(), "第二段也应该另起一行。")
        self.assertTrue(all(line.startswith("\u3000\u3000") for line in lines))

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

        jobs = json.loads(await self.plugin.handle_novel_list_jobs("12", "0"))
        self.assertEqual(jobs["returned_count"], 12)
        self.assertLessEqual(len(jobs["jobs"]), self.plugin.max_tool_preview_items)
        self.assertGreater(jobs["omitted_from_inline_count"], 0)
        self.assertIn("report_path", jobs)
        self.assertTrue(Path(jobs["report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
