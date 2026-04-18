from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from parsel import Selector

from astrbot_plugin_webnovel_downloader.core.book_resolution_service import BookResolutionService
from astrbot_plugin_webnovel_downloader.core.rule_engine import RuleEngine, RuleEngineConfig, RuleEngineError
from astrbot_plugin_webnovel_downloader.core.search_service import SearchService, SearchServiceConfig
from astrbot_plugin_webnovel_downloader.core.source_health_store import SourceHealthStore
from astrbot_plugin_webnovel_downloader.core.source_registry import SourceRegistry


class _SearchRegistry(object):
    def __init__(self, summaries, sources):
        self._summaries = {
            str(source_id): dict(summary)
            for source_id, summary in dict(summaries or {}).items()
        }
        self._sources = {
            str(source_id): dict(source)
            for source_id, source in dict(sources or {}).items()
        }

    def load_enabled_source_summaries(self, source_ids=None, include_disabled=False):
        selected = set(str(item) for item in list(source_ids or []))
        result = []
        for source_id, summary in self._summaries.items():
            if selected and source_id not in selected:
                continue
            if not include_disabled and not summary.get("enabled", True):
                continue
            result.append(dict(summary))
        return result

    def load_enabled_sources(self, source_ids=None, include_disabled=False):
        selected = set(str(item) for item in list(source_ids or []))
        result = []
        for source_id, source in self._sources.items():
            summary = self._summaries.get(source_id, {})
            if selected and source_id not in selected:
                continue
            if not include_disabled and not summary.get("enabled", True):
                continue
            result.append(dict(source))
        return result

    def get_source_summary(self, source_id):
        return dict(self._summaries[str(source_id)])


class _FakeSearchEngine(object):
    def __init__(self, results):
        self.results = {
            str(source_id): [dict(item) for item in list(items or [])]
            for source_id, items in dict(results or {}).items()
        }
        self.calls = []

    def search_books(self, source, keyword, limit):
        self.calls.append((str(source["source_id"]), keyword, limit))
        return list(self.results.get(str(source["source_id"]), []))


class _FakeResolutionSearchService(object):
    def __init__(self, results):
        self.results = [dict(item) for item in list(results or [])]

    def search(self, keyword, source_ids=None, limit=20, include_disabled=False):
        del keyword, source_ids, limit, include_disabled
        return {
            "keyword": "测试书",
            "searched_sources": len({item.get("source_id") for item in self.results}),
            "successful_sources": len({item.get("source_id") for item in self.results}),
            "result_count": len(self.results),
            "results": [dict(item) for item in self.results],
            "errors": [],
        }


class _ResolutionRegistry(object):
    def __init__(self, summaries):
        self._summaries = {
            str(source_id): dict(summary)
            for source_id, summary in dict(summaries or {}).items()
        }

    def get_source_summary(self, source_id):
        return dict(self._summaries[str(source_id)])


class LegadoPhase5Test(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_import_marks_webview_download_as_unsupported(self):
        registry = SourceRegistry(self.base_dir)
        payload = [
            {
                "bookSourceName": "webView目录源",
                "bookSourceUrl": "https://example.com",
                "searchUrl": "https://example.com/search?q={{key}}",
                "ruleSearch": {
                    "bookList": ".book",
                    "name": "a@text",
                    "bookUrl": "a@href",
                },
                "ruleBookInfo": {"name": "h1@text"},
                "ruleToc": {
                    "chapterList": ".list@li a",
                    "chapterName": "text",
                    "chapterUrl": "href##$##,{'webView': true}",
                },
                "ruleContent": {"content": "#content@html"},
            }
        ]

        result = registry.import_sources_from_text(json.dumps(payload, ensure_ascii=False))

        source = result["sources"][0]
        self.assertTrue(source["supports_search"])
        self.assertFalse(source["supports_download"])
        self.assertFalse(source["search_uses_webview"])
        self.assertTrue(source["download_uses_webview"])
        self.assertTrue(any("webView" in issue for issue in source["issues"]))

    def test_rule_engine_parses_single_quote_request_options_and_rejects_webview(self):
        engine = RuleEngine(RuleEngineConfig())

        base_url, options = engine._split_request_options("https://example.com/book,{'webView': true}")

        self.assertEqual(base_url, "https://example.com/book")
        self.assertEqual(options, {"webView": True})
        with self.assertRaisesRegex(RuleEngineError, "webView"):
            engine._fetch_text("https://example.com/book,{'webView': true}", {})

    def test_rule_engine_sanitizes_legado_control_headers(self):
        engine = RuleEngine(RuleEngineConfig())

        headers = engine._normalize_request_headers(
            {
                "@js": "JSON.stringify({\"Referer\":baseUrl})",
                "Referer": "https://example.com",
                "X-Test": "ok",
                "X-JS": "@js:return 1",
            }
        )

        self.assertEqual(headers, {"Referer": "https://example.com", "X-Test": "ok"})

    def test_rule_engine_supports_common_legado_selector_modifiers(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = Selector(
            text="""
            <html>
              <body>
                <div class="list">
                  <li><a href="/1">第一章</a></li>
                  <li><a href="/2">第二章</a></li>
                </div>
                <div class="book w">
                  <span>分类</span>
                  <span>状态</span>
                  <span>时间</span>
                </div>
              </body>
            </html>
            """
        )

        chapter_nodes = engine._select_many("html", payload, ".list!0@li a")
        kind_values = engine._select_many("html", payload, "class.book w@span[1,2]@text")

        self.assertEqual(len(chapter_nodes), 2)
        self.assertEqual(kind_values, ["状态", "时间"])

    def test_search_service_uses_runtime_health_to_skip_broken_search_and_hide_broken_download(self):
        health_store = SourceHealthStore(self.base_dir / "source_health.json")
        health_store.record_failure(
            "broken-search",
            "search",
            error_code="timeout",
            error_summary="最近搜索超时",
        )
        health_store.record_success("good-source", "search", summary="搜索探测成功")
        health_store.record_failure(
            "good-source",
            "preflight",
            error_code="preflight_failed",
            error_summary="未解析到目录",
        )
        registry = _SearchRegistry(
            {
                "broken-search": {
                    "source_id": "broken-search",
                    "name": "坏搜索源",
                    "enabled": True,
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                },
                "good-source": {
                    "source_id": "good-source",
                    "name": "好源",
                    "enabled": True,
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                },
            },
            {
                "broken-search": {"source_id": "broken-search", "name": "坏搜索源"},
                "good-source": {"source_id": "good-source", "name": "好源"},
            },
        )
        engine = _FakeSearchEngine(
            {
                "broken-search": [{"source_id": "broken-search", "title": "测试书"}],
                "good-source": [{"source_id": "good-source", "title": "测试书"}],
            }
        )
        service = SearchService(
            registry,
            engine,
            SearchServiceConfig(max_workers=1, time_budget_seconds=5.0),
            source_health_store=health_store,
        )

        payload = service.search("测试书", limit=5)

        self.assertEqual(engine.calls, [("good-source", "测试书", 5)])
        self.assertEqual(payload["skipped_sources"][0]["source_id"], "broken-search")
        self.assertEqual(payload["results"][0]["source_id"], "good-source")
        self.assertFalse(payload["results"][0]["supports_download"])
        self.assertEqual(payload["results"][0]["preflight_health_state"], "broken")

    def test_book_resolution_uses_runtime_health_skip_reason(self):
        health_store = SourceHealthStore(self.base_dir / "source_health.json")
        health_store.record_failure(
            "runtime-broken",
            "preflight",
            error_code="preflight_failed",
            error_summary="目录预检失败",
        )
        registry = _ResolutionRegistry(
            {
                "runtime-broken": {
                    "source_id": "runtime-broken",
                    "name": "运行时坏源",
                    "supports_download": True,
                    "issues": [],
                },
                "healthy": {
                    "source_id": "healthy",
                    "name": "健康源",
                    "supports_download": True,
                    "issues": [],
                },
            }
        )
        search_service = _FakeResolutionSearchService(
            [
                {
                    "source_id": "runtime-broken",
                    "source_name": "运行时坏源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/broken",
                    "supports_download": True,
                },
                {
                    "source_id": "healthy",
                    "source_name": "健康源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/good",
                    "supports_download": True,
                },
            ]
        )
        resolver = BookResolutionService(registry, search_service, health_store)

        payload = resolver.resolve("测试书", author="测试作者", limit=10)

        self.assertEqual(payload["candidates"][0]["source_id"], "healthy")
        self.assertEqual(payload["skipped_candidates"][0]["source_id"], "runtime-broken")
        self.assertIn("目录预检失败", payload["skipped_candidates"][0]["skip_reason"])


if __name__ == "__main__":
    unittest.main()
