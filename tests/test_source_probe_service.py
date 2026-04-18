from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.source_health_store import SourceHealthStore
from astrbot_plugin_webnovel_downloader.core.source_probe_service import (
    SourceProbeService,
    SourceProbeServiceConfig,
)


class _FakeRegistry(object):
    def __init__(self, summaries, sources):
        self._summaries = summaries
        self._sources = sources

    def get_source_summary(self, source_id):
        return dict(self._summaries[source_id])

    def load_normalized_source(self, source_id):
        return dict(self._sources[source_id])


class _FakeEngine(object):
    def __init__(self):
        self.search_calls = []
        self.preflight_calls = []
        self.search_results = {}
        self.search_errors = {}
        self.preflight_results = {}
        self.preflight_errors = {}

    def search_books(self, source, keyword, limit=3):
        self.search_calls.append((source["source_id"], keyword, limit))
        error = self.search_errors.get(source["source_id"])
        if error is not None:
            raise error
        return list(self.search_results.get(source["source_id"], []))

    def build_book_download_plan(self, source, book_url, fallback_title=""):
        self.preflight_calls.append((source["source_id"], book_url, fallback_title))
        error = self.preflight_errors.get(source["source_id"])
        if error is not None:
            raise error
        return dict(
            self.preflight_results.get(
                source["source_id"],
                {
                    "book_url": book_url,
                    "book_name": fallback_title or "样本书",
                    "toc": [{"title": "第1章", "url": "https://example.com/chapter-1"}],
                },
            )
        )


class _FakeSourceDownloadService(object):
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def sample_book(self, plan, chapter_count=None, min_content_chars=None):
        del chapter_count, min_content_chars
        self.calls.append(dict(plan))
        if self.error is not None:
            raise self.error
        return {
            "sampled_chapter_count": 1,
            "sampled_chapters": [
                {
                    "index": 0,
                    "title": "第一章",
                    "url": "https://example.com/chapter-1",
                    "content_chars": 128,
                }
            ],
        }


class SourceProbeServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.store = SourceHealthStore(self.base_dir / "source_health.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_enqueue_deduplicates_and_records_search_success(self):
        registry = _FakeRegistry(
            {
                "search-only": {
                    "source_id": "search-only",
                    "supports_search": True,
                    "supports_download": False,
                    "issues": ["当前源不支持下载"],
                }
            },
            {
                "search-only": {
                    "source_id": "search-only",
                    "name": "只搜不下",
                }
            },
        )
        engine = _FakeEngine()
        engine.search_results["search-only"] = []
        service = SourceProbeService(
            registry,
            engine,
            self.store,
            SourceProbeServiceConfig(max_workers=1, probe_keywords=("诡秘之主",)),
        )

        result = service.enqueue_sources(["search-only", "search-only"])

        self.assertEqual(result["queued_count"], 1)
        self.assertTrue(service.wait_for_idle(2.0))
        self.assertEqual(len(engine.search_calls), 1)
        entry = self.store.get_source_health("search-only")
        self.assertEqual(entry["search"]["state"], "healthy")
        self.assertEqual(entry["preflight"]["state"], "unsupported")
        self.assertEqual(entry["download"]["state"], "unsupported")

    def test_probe_records_preflight_success_when_sample_found(self):
        registry = _FakeRegistry(
            {
                "full": {
                    "source_id": "full",
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                }
            },
            {
                "full": {
                    "source_id": "full",
                    "name": "完整源",
                }
            },
        )
        engine = _FakeEngine()
        engine.search_results["full"] = [
            {
                "title": "样本书",
                "book_url": "https://example.com/book/1",
            }
        ]
        service = SourceProbeService(
            registry,
            engine,
            self.store,
            SourceProbeServiceConfig(max_workers=1, probe_keywords=("诡秘之主",)),
        )

        service.enqueue_sources(["full"])
        self.assertTrue(service.wait_for_idle(2.0))

        entry = self.store.get_source_health("full")
        self.assertEqual(entry["search"]["state"], "healthy")
        self.assertEqual(entry["preflight"]["state"], "healthy")
        self.assertEqual(entry["download"]["state"], "unknown")
        self.assertEqual(entry["preflight"]["sample_book_url"], "https://example.com/book/1")
        self.assertEqual(len(engine.preflight_calls), 1)

    def test_probe_can_record_download_sample_success_when_sampler_is_available(self):
        registry = _FakeRegistry(
            {
                "full": {
                    "source_id": "full",
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                }
            },
            {
                "full": {
                    "source_id": "full",
                    "name": "完整源",
                }
            },
        )
        engine = _FakeEngine()
        engine.search_results["full"] = [
            {
                "title": "样本书",
                "book_url": "https://example.com/book/1",
            }
        ]
        downloader = _FakeSourceDownloadService()
        service = SourceProbeService(
            registry,
            engine,
            self.store,
            SourceProbeServiceConfig(max_workers=1, probe_keywords=("诡秘之主",)),
            source_download_service=downloader,
        )

        service.enqueue_sources(["full"])
        self.assertTrue(service.wait_for_idle(2.0))

        entry = self.store.get_source_health("full")
        self.assertEqual(entry["download"]["state"], "healthy")
        self.assertEqual(entry["download"]["note"], "正文抽样成功")
        self.assertEqual(len(downloader.calls), 1)

    def test_probe_leaves_preflight_unknown_when_search_has_no_results(self):
        registry = _FakeRegistry(
            {
                "no-sample": {
                    "source_id": "no-sample",
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                }
            },
            {
                "no-sample": {
                    "source_id": "no-sample",
                    "name": "无样本源",
                }
            },
        )
        engine = _FakeEngine()
        engine.search_results["no-sample"] = []
        service = SourceProbeService(
            registry,
            engine,
            self.store,
            SourceProbeServiceConfig(max_workers=1, probe_keywords=("诡秘之主",)),
        )

        service.enqueue_sources(["no-sample"])
        self.assertTrue(service.wait_for_idle(2.0))

        entry = self.store.get_source_health("no-sample")
        self.assertEqual(entry["search"]["state"], "healthy")
        self.assertEqual(entry["preflight"]["state"], "unknown")
        self.assertEqual(entry["download"]["state"], "unknown")
        self.assertEqual(len(engine.preflight_calls), 0)

    def test_probe_records_failure_on_search_exception(self):
        registry = _FakeRegistry(
            {
                "broken": {
                    "source_id": "broken",
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                }
            },
            {
                "broken": {
                    "source_id": "broken",
                    "name": "坏源",
                }
            },
        )
        engine = _FakeEngine()
        engine.search_errors["broken"] = RuntimeError("网络错误")
        service = SourceProbeService(
            registry,
            engine,
            self.store,
            SourceProbeServiceConfig(max_workers=1, probe_keywords=("诡秘之主",)),
        )

        service.enqueue_sources(["broken"])
        self.assertTrue(service.wait_for_idle(2.0))

        entry = self.store.get_source_health("broken")
        self.assertEqual(entry["search"]["state"], "broken")
        self.assertEqual(entry["preflight"]["state"], "unknown")
        self.assertEqual(entry["download"]["state"], "unknown")

    def test_probe_marks_static_unsupported_without_network_calls(self):
        registry = _FakeRegistry(
            {
                "unsupported": {
                    "source_id": "unsupported",
                    "supports_search": False,
                    "supports_download": False,
                    "issues": ["ruleSearch 含 JS 规则"],
                }
            },
            {
                "unsupported": {
                    "source_id": "unsupported",
                    "name": "JS源",
                }
            },
        )
        engine = _FakeEngine()
        service = SourceProbeService(
            registry,
            engine,
            self.store,
            SourceProbeServiceConfig(max_workers=1, probe_keywords=("诡秘之主",)),
        )

        service.enqueue_sources(["unsupported"])
        self.assertTrue(service.wait_for_idle(2.0))

        entry = self.store.get_source_health("unsupported")
        self.assertEqual(entry["search"]["state"], "unsupported")
        self.assertEqual(entry["preflight"]["state"], "unsupported")
        self.assertEqual(entry["download"]["state"], "unsupported")
        self.assertEqual(engine.search_calls, [])


if __name__ == "__main__":
    unittest.main()
