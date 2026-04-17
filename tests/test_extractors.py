from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.download_manager import (
    NovelDownloadManager,
    RuntimeConfig,
)
from astrbot_plugin_webnovel_downloader.core.extractors import (
    Extractor,
    FallbackRuleExtractor,
)
from astrbot_plugin_webnovel_downloader.core.rule_engine import (
    RuleEngine,
    RuleEngineConfig,
)
from astrbot_plugin_webnovel_downloader.core.search_service import (
    SearchService,
    SearchServiceConfig,
)
from astrbot_plugin_webnovel_downloader.core.source_downloader import SourceDownloadService


class _FakeRegistry(object):
    def __init__(self, summaries, sources):
        self._summaries = {
            str(item["source_id"]): dict(item)
            for item in list(summaries or [])
        }
        self._sources = {
            str(item["source_id"]): dict(item)
            for item in list(sources or [])
        }

    def load_enabled_source_summaries(self, source_ids=None, include_disabled=False):
        del include_disabled
        if source_ids is None:
            return [dict(item) for item in self._summaries.values()]
        wanted = {str(item) for item in source_ids}
        return [
            dict(item)
            for source_id, item in self._summaries.items()
            if source_id in wanted
        ]

    def load_enabled_sources(self, source_ids=None, include_disabled=False):
        del include_disabled
        if source_ids is None:
            return [dict(item) for item in self._sources.values()]
        wanted = {str(item) for item in source_ids}
        return [
            dict(item)
            for source_id, item in self._sources.items()
            if source_id in wanted
        ]

    def get_source_summary(self, source_id):
        return dict(self._summaries[source_id])

    def load_normalized_source(self, source_id):
        return dict(self._sources[source_id])


class _SpyRuleEngine(RuleEngine):
    def __init__(self):
        super().__init__(RuleEngineConfig())
        self.calls = []

    def search_books(self, source, keyword, limit=20):
        self.calls.append(("search_books", source["source_id"], keyword, limit))
        return [
            {
                "source_id": source["source_id"],
                "source_name": source.get("name", ""),
                "title": keyword,
                "author": "测试作者",
                "book_url": "https://example.com/book/1",
                "cover_url": "",
                "intro": "测试简介",
                "kind": "玄幻",
                "last_chapter": "第一章",
                "word_count": "1000",
                "match_keyword": keyword,
            }
        ]

    def build_book_download_plan(self, source, book_url, fallback_title=""):
        self.calls.append(
            (
                "build_book_download_plan",
                source["source_id"],
                book_url,
                fallback_title,
            )
        )
        return {
            "book_url": book_url,
            "toc_url": book_url + "#toc",
            "book_name": fallback_title or "测试书",
            "author": "测试作者",
            "intro": "测试简介",
            "toc": [
                {
                    "index": 0,
                    "title": "第一章",
                    "url": "https://example.com/chapter-1",
                }
            ],
        }

    def fetch_chapter_content(self, source, chapter_url, fallback_title="", max_pages=5):
        self.calls.append(
            (
                "fetch_chapter_content",
                source["source_id"],
                chapter_url,
                fallback_title,
                max_pages,
            )
        )
        return {
            "title": fallback_title or "第一章",
            "content": "这里是正文",
            "encoding": "utf-8",
        }


class FallbackRuleExtractorTest(unittest.TestCase):
    def setUp(self):
        self.source = {
            "source_id": "source-1",
            "name": "测试源",
            "source_url": "https://example.com",
        }

    def test_delegates_new_and_legacy_entrypoints_to_rule_engine(self):
        engine = _SpyRuleEngine()
        extractor = FallbackRuleExtractor(engine)

        self.assertIsInstance(extractor, Extractor)

        new_search = extractor.search(self.source, "新接口关键词", limit=8)
        legacy_search = extractor.search_books(self.source, "旧接口关键词", limit=4)
        new_plan = extractor.preflight(
            self.source,
            "https://example.com/book/new",
            fallback_title="新接口书名",
        )
        legacy_plan = extractor.build_book_download_plan(
            self.source,
            "https://example.com/book/legacy",
            fallback_title="旧接口书名",
        )
        new_content = extractor.fetch_content(
            self.source,
            "https://example.com/chapter/new",
            fallback_title="新接口章节",
            max_pages=2,
        )
        legacy_content = extractor.fetch_chapter_content(
            self.source,
            "https://example.com/chapter/legacy",
            fallback_title="旧接口章节",
            max_pages=3,
        )

        self.assertEqual(new_search[0]["title"], "新接口关键词")
        self.assertEqual(legacy_search[0]["title"], "旧接口关键词")
        self.assertEqual(new_plan["book_name"], "新接口书名")
        self.assertEqual(legacy_plan["book_name"], "旧接口书名")
        self.assertEqual(new_content["title"], "新接口章节")
        self.assertEqual(legacy_content["title"], "旧接口章节")
        self.assertEqual(
            engine.calls,
            [
                ("search_books", "source-1", "新接口关键词", 8),
                ("search_books", "source-1", "旧接口关键词", 4),
                (
                    "build_book_download_plan",
                    "source-1",
                    "https://example.com/book/new",
                    "新接口书名",
                ),
                (
                    "build_book_download_plan",
                    "source-1",
                    "https://example.com/book/legacy",
                    "旧接口书名",
                ),
                (
                    "fetch_chapter_content",
                    "source-1",
                    "https://example.com/chapter/new",
                    "新接口章节",
                    2,
                ),
                (
                    "fetch_chapter_content",
                    "source-1",
                    "https://example.com/chapter/legacy",
                    "旧接口章节",
                    3,
                ),
            ],
        )

    def test_search_service_accepts_extractor_without_service_changes(self):
        engine = _SpyRuleEngine()
        extractor = FallbackRuleExtractor(engine)
        registry = _FakeRegistry(
            summaries=[
                {
                    "source_id": "source-1",
                    "name": "测试源",
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                }
            ],
            sources=[self.source],
        )
        service = SearchService(
            registry,
            extractor,
            SearchServiceConfig(max_workers=1, time_budget_seconds=2.0),
        )

        result = service.search("诡秘之主", source_ids=["source-1"], limit=5)

        self.assertEqual(result["successful_sources"], 1)
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["title"], "诡秘之主")
        self.assertIn(("search_books", "source-1", "诡秘之主", 5), engine.calls)


class FallbackRuleExtractorDownloadIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = NovelDownloadManager(Path(self.tempdir.name), RuntimeConfig())
        self.source = {
            "source_id": "source-1",
            "name": "测试源",
            "source_url": "https://example.com",
        }
        self.registry = _FakeRegistry(
            summaries=[
                {
                    "source_id": "source-1",
                    "name": "测试源",
                    "source_url": "https://example.com",
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                }
            ],
            sources=[self.source],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_source_downloader_accepts_extractor_for_preflight_and_download(self):
        engine = _SpyRuleEngine()
        extractor = FallbackRuleExtractor(engine)
        service = SourceDownloadService(self.registry, extractor, self.manager)

        plan = service.preflight_book("source-1", "https://example.com/book/1", "测试书")
        job_info = service.create_job_from_plan(plan)
        status = service.resume_book_job(job_info["job_id"], auto_assemble=False)

        self.assertEqual(plan["book_name"], "测试书")
        self.assertEqual(plan["toc_count"], 1)
        self.assertEqual(status["state"], "downloaded")
        self.assertEqual(status["completed_chapters"], 1)
        self.assertEqual(self.manager.get_missing_chapters(job_info["job_id"]), [])
        self.assertIn(
            (
                "build_book_download_plan",
                "source-1",
                "https://example.com/book/1",
                "测试书",
            ),
            engine.calls,
        )
        self.assertIn(
            (
                "fetch_chapter_content",
                "source-1",
                "https://example.com/chapter-1",
                "第一章",
                5,
            ),
            engine.calls,
        )


if __name__ == "__main__":
    unittest.main()
