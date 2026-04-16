from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Dict, get_args, get_origin


SUPPORTED_TOOL_TYPES = {str}


def _validate_tool_signature(func):
    annotations = getattr(func, "__annotations__", {})
    for name, annotation in annotations.items():
        if name in ("return", "self"):
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

    async def test_llm_tools_end_to_end(self):
        source_json = json.dumps(
            [
                {
                    "bookSourceName": "测试JSON源",
                    "bookSourceUrl": "https://example.com",
                    "searchUrl": (self.base_dir / "search.json").resolve().as_uri(),
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "author": "author",
                        "bookUrl": "url",
                        "intro": "intro",
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
                                "url": "/book/xzhdx",
                                "intro": "测试简介",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        import_result = json.loads(await self.plugin.novel_import_sources(source_json))
        self.assertEqual(import_result["imported_count"], 1)

        listed_sources = json.loads(await self.plugin.novel_list_sources())
        self.assertEqual(len(listed_sources), 1)
        self.assertEqual(listed_sources[0]["name"], "测试JSON源")

        search_result = json.loads(await self.plugin.novel_search_books("雪中"))
        self.assertEqual(search_result["searched_sources"], 1)
        self.assertGreaterEqual(search_result["result_count"], 1)
        self.assertEqual(search_result["results"][0]["title"], "雪中悍刀行")
        self.assertEqual(
            search_result["results"][0]["book_url"],
            "https://example.com/book/xzhdx",
        )

        chapters_dir = self.base_dir / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "1.html").write_text(
            "<html><head><title>第一章 降生</title></head>"
            "<body><h1>第一章 降生</h1><div id='content'><p>这是第一章。</p></div></body></html>",
            encoding="utf-8",
        )
        (chapters_dir / "2.html").write_text(
            "<html><head><title>第二章 练剑</title></head>"
            "<body><h1>第二章 练剑</h1><div id='content'><p>这是第二章。</p></div></body></html>",
            encoding="utf-8",
        )

        preview = json.loads(
            await self.plugin.novel_fetch_preview(
                (chapters_dir / "1.html").resolve().as_uri(),
                "",
                "200",
            )
        )
        self.assertIn("第一章 降生", preview["text_preview"])

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
        start_text = await self.plugin.novel_start_download(
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

        status_text = await self.plugin.novel_download_status(job_id)
        self.assertIn("状态: assembled", status_text)

        assembled_text = await self.plugin.novel_assemble_book(job_id, "false")
        self.assertIn("状态: assembled", assembled_text)

        output_path = self.plugin.manager.output_dir / "测试小说.txt"
        self.assertTrue(output_path.exists())
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("第一章 降生", content)
        self.assertIn("这是第二章。", content)


if __name__ == "__main__":
    unittest.main()
