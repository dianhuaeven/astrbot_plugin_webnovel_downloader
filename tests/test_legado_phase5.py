from __future__ import annotations

import hashlib
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

        chapter_nodes = engine._select_many("html", payload, ".list@li a")
        kind_values = engine._select_many("html", payload, "class.book w@span[1,2]@text")

        self.assertEqual(len(chapter_nodes), 2)
        self.assertEqual(kind_values, ["状态", "时间"])

    def test_rule_engine_supports_tag_prefix_in_html_selectors(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = Selector(
            text="""
            <html>
              <body>
                <div class="detail">
                  <p class="author">作者：<a href="/author/test">测试作者</a></p>
                </div>
                <ul class="read">
                  <li><a href="/1">第一章</a></li>
                  <li><a href="/2">第二章</a></li>
                </ul>
              </body>
            </html>
            """
        )

        chapter_nodes = engine._select_many("html", payload, "class.read@tag.li")
        author_values = engine._select_many("html", payload, "class.author@tag.a@text")

        self.assertEqual(len(chapter_nodes), 2)
        self.assertEqual(author_values, ["测试作者"])

    def test_rule_engine_supports_literal_template_fragments_and_modifier_only_steps(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = Selector(
            text="""
            <html>
              <body>
                <div class="list">
                  <span>甲</span>
                  <span>乙</span>
                  <span>丙</span>
                </div>
              </body>
            </html>
            """
        )

        rendered = engine._render_template("prefix{{'\\n'+'\\u200b'}}suffix", {})
        last_value = engine._select_many("html", payload, ".list@span&&.-1&&@text")
        sliced_values = engine._select_many("html", payload, ".list@span&&.0:-1&&@text")
        excluded_values = engine._select_many("html", payload, ".list@span&&!0,1,2&&@text")

        self.assertEqual(rendered, "prefix\n\u200bsuffix")
        self.assertEqual(last_value, ["丙"])
        self.assertEqual(sliced_values, ["甲", "乙", "丙"])
        self.assertEqual(excluded_values, [])

    def test_rule_engine_supports_bracket_slice_after_html_selector(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = Selector(
            text="""
            <html>
              <body>
                <dl class="chapters">
                  <dd>头部</dd>
                  <dd>第一章</dd>
                  <dd>第二章</dd>
                  <dd>尾部</dd>
                </dl>
              </body>
            </html>
            """
        )

        values = engine._select_many("html", payload, ".chapters@dd[1:-2]&&@text")

        self.assertEqual(values, ["第一章", "第二章"])

    def test_rule_engine_supports_put_and_get_context_variables(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = {
            "novel": {
                "name": "测试书",
                "id": 123,
            },
            "id": "chapter-1",
        }
        rule_context = {}

        title = engine._extract_scalar(
            "json",
            payload,
            "$.novel.name@put:{bid:$.novel.id}",
            rule_context=rule_context,
        )
        direct_value = engine._extract_scalar(
            "json",
            payload,
            "@get:{bid}",
            rule_context=rule_context,
        )
        chapter_url = engine._extract_scalar(
            "json",
            payload,
            "https://example.com/book/@get:{bid}/{{$.id}}",
            rule_context=rule_context,
        )

        self.assertEqual(title, "测试书")
        self.assertEqual(rule_context["bid"], "123")
        self.assertEqual(direct_value, "123")
        self.assertEqual(chapter_url, "https://example.com/book/123/chapter-1")

    def test_rule_engine_init_can_switch_json_context_for_followup_fields(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = {
            "data": {
                "bookInfo": {
                    "bookId": 42,
                    "bookName": "秒速五厘米",
                    "authorName": "新海诚",
                }
            }
        }
        rule_context = {}

        scoped_payload = engine._run_rule_init("json", payload, "data.bookInfo", rule_context)
        title = engine._extract_scalar("json", scoped_payload, "bookName", rule_context=rule_context)
        author = engine._extract_scalar("json", scoped_payload, "authorName", rule_context=rule_context)
        toc_url = engine._extract_scalar(
            "json",
            scoped_payload,
            "/catalog?bookid={{$.bookId}}",
            rule_context=rule_context,
        )

        self.assertEqual(title, "秒速五厘米")
        self.assertEqual(author, "新海诚")
        self.assertEqual(toc_url, "/catalog?bookid=42")

    def test_rule_engine_supports_bare_html_attr_rules(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = Selector(text='<html><body><a href="/chapter-1">第一章</a></body></html>')
        link = engine._select_many("html", payload, "a")[0]

        href = engine._extract_scalar("html", link, "href")
        text = engine._extract_scalar("html", link, "text")
        text_nodes = engine._extract_scalar("html", link, "textNodes")

        self.assertEqual(href, "/chapter-1")
        self.assertEqual(text, "第一章")
        self.assertEqual(text_nodes, "第一章")

    def test_rule_engine_filters_non_chapter_toc_links_before_returning_plan(self):
        engine = RuleEngine(RuleEngineConfig())
        toc_path = self.base_dir / "filtered-toc.html"
        toc_desc_path = self.base_dir / "filtered-toc-desc.html"
        chapter_1_path = self.base_dir / "filtered-chapter-1.html"
        chapter_2_path = self.base_dir / "filtered-chapter-2.html"
        detail_path = self.base_dir / "filtered-detail.html"

        chapter_1_path.write_text(
            "<html><body><div id='content'>正文1</div></body></html>",
            encoding="utf-8",
        )
        chapter_2_path.write_text(
            "<html><body><div id='content'>正文2</div></body></html>",
            encoding="utf-8",
        )
        toc_desc_path.write_text(
            "<html><body>倒序目录占位</body></html>",
            encoding="utf-8",
        )
        toc_path.write_text(
            "<html><body><div id='list'><dl>"
            "<a href='{detail}'>兼容测试书</a>"
            "<a href='{toc}'>[正序]</a>"
            "<a href='{toc_desc}'>[倒序]</a>"
            "<a href='{chapter1}'>第一章</a>"
            "<a href='{chapter2}'>第二章</a>"
            "</dl></div></body></html>".format(
                detail=detail_path.resolve().as_uri(),
                toc=toc_path.resolve().as_uri(),
                toc_desc=toc_desc_path.resolve().as_uri(),
                chapter1=chapter_1_path.resolve().as_uri(),
                chapter2=chapter_2_path.resolve().as_uri(),
            ),
            encoding="utf-8",
        )
        detail_path.write_text(
            "<html><head>"
            "<meta property='og:novel:book_name' content='兼容测试书' />"
            "<meta property='og:novel:author' content='测试作者' />"
            "</head><body>"
            "<a id='toc-link' href='{toc}'>全文目录</a>"
            "</body></html>".format(toc=toc_path.resolve().as_uri()),
            encoding="utf-8",
        )
        source = {
            "source_id": "filter-source",
            "source_url": "https://example.com",
            "rule_book_info": {
                "name": "[property=og:novel:book_name]@content",
                "author": "[property=og:novel:author]@content",
                "tocUrl": "#toc-link@href",
            },
            "rule_toc": {
                "chapterList": "#list dl a",
                "chapterName": "text",
                "chapterUrl": "@href",
            },
            "rule_content": {"content": "#content&&text"},
        }

        plan = engine.build_book_download_plan(
            source,
            detail_path.resolve().as_uri(),
            "",
        )

        self.assertEqual(plan["book_name"], "兼容测试书")
        self.assertEqual(plan["author"], "测试作者")
        self.assertEqual([item["title"] for item in plan["toc"]], ["第一章", "第二章"])
        self.assertEqual(plan["toc"][0]["index"], 0)
        self.assertEqual(plan["toc"][1]["index"], 1)

    def test_rule_engine_supports_lightweight_js_templates_and_transforms(self):
        engine = RuleEngine(RuleEngineConfig())
        payload = {"name": "A B C"}

        rendered = engine._render_rule_template(
            "json",
            payload,
            "{{java.md5Encode('abc')}}",
        )
        transformed = engine._extract_scalar(
            "json",
            payload,
            "$.name@js:result.replace(/\\s+/g, '')",
        )

        self.assertEqual(rendered, hashlib.md5(b"abc").hexdigest())
        self.assertEqual(transformed, "ABC")

    def test_search_service_keeps_broken_runtime_sources_visible_but_lower_priority(self):
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

        self.assertEqual(
            engine.calls,
            [("good-source", "测试书", 5), ("broken-search", "测试书", 5)],
        )
        self.assertEqual(payload["skipped_sources"], [])
        self.assertEqual(payload["results"][0]["source_id"], "good-source")
        self.assertTrue(payload["results"][0]["supports_download"])
        self.assertEqual(payload["results"][0]["preflight_health_state"], "broken")
        self.assertEqual(payload["results"][1]["source_id"], "broken-search")
        self.assertEqual(payload["results"][1]["search_health_state"], "broken")

    def test_book_resolution_keeps_runtime_broken_source_as_lower_priority_candidate(self):
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
        self.assertEqual(payload["candidates"][1]["source_id"], "runtime-broken")
        self.assertEqual(payload["candidates"][1]["preflight_health_state"], "broken")
        self.assertTrue(payload["candidates"][1]["supports_download"])
        self.assertEqual(payload["skipped_candidates"], [])


if __name__ == "__main__":
    unittest.main()
