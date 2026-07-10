from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.download_manager import (
    ExtractionRules,
    NovelDownloadManager,
    RuntimeConfig,
)
from astrbot_plugin_webnovel_downloader.core.source_downloader import (
    SourceDownloadConfig,
    SourceDownloadService,
)
from astrbot_plugin_webnovel_downloader.core.rule_engine import RuleEngineError
from astrbot_plugin_webnovel_downloader.core.source_health_store import (
    SourceHealthStore,
)


class _FakeRegistry(object):
    def __init__(self, summaries, normalized):
        self._summaries = dict(summaries or {})
        self._normalized = dict(normalized or {})

    def get_source_summary(self, source_id):
        return dict(self._summaries[source_id])

    def load_normalized_source(self, source_id):
        return dict(self._normalized[source_id])


class _FakeEngine(object):
    def __init__(self):
        self.chapter_calls = []

    def fetch_chapter_content(
        self, source, chapter_url, fallback_title="", max_pages=5
    ):
        del source, max_pages
        self.chapter_calls.append((chapter_url, fallback_title))
        if "short" in chapter_url:
            return {"title": fallback_title, "content": "太短", "encoding": "utf-8"}
        return {
            "title": fallback_title or "第一章",
            "content": "这里是足够长的测试正文内容，用来通过正文抽样。",
            "encoding": "utf-8",
        }


class _AlwaysForbiddenEngine(object):
    def __init__(self):
        self.chapter_calls = []

    def fetch_chapter_content(
        self, source, chapter_url, fallback_title="", max_pages=5
    ):
        del source, max_pages
        self.chapter_calls.append((chapter_url, fallback_title))
        raise RuntimeError("HTTP 403: Forbidden")


class _BookSpecificFailureEngine(object):
    def fetch_chapter_content(
        self, source, chapter_url, fallback_title="", max_pages=5
    ):
        del source, chapter_url, fallback_title, max_pages
        raise RuntimeError("当前书籍未解析到正文")


class _RuleContextEngine(object):
    def __init__(self):
        self.chapter_calls = []
        self.preflight_calls = []

    def build_book_download_plan(
        self, source, book_url, fallback_title="", rule_context=None
    ):
        del source
        self.preflight_calls.append(
            {
                "book_url": book_url,
                "fallback_title": fallback_title,
                "rule_context": dict(rule_context or {}),
            }
        )
        return {
            "book_url": book_url,
            "toc_url": book_url + "#toc",
            "book_name": fallback_title or "测试书",
            "author": "测试作者",
            "intro": "",
            "toc": [
                {
                    "index": 0,
                    "title": "第一章",
                    "url": book_url + "/chapter-1",
                    "_rule_vars": {"chapter_token": "token-1"},
                }
            ],
            "_rule_vars": dict(rule_context or {}),
        }

    def fetch_chapter_content(
        self,
        source,
        chapter_url,
        fallback_title="",
        max_pages=5,
        rule_context=None,
    ):
        del source, max_pages
        current_rule_context = dict(rule_context or {})
        self.chapter_calls.append((chapter_url, fallback_title, current_rule_context))
        if current_rule_context.get("chapter_token") != "token-1":
            raise RuntimeError("missing chapter rule context")
        return {
            "title": fallback_title or "第一章",
            "content": "这里是依赖 chapter_token 才能抓到的正文内容。",
            "encoding": "utf-8",
        }


class _BadSampleEngine(object):
    def fetch_chapter_content(
        self, source, chapter_url, fallback_title="", max_pages=5
    ):
        del source, chapter_url, fallback_title, max_pages
        return {
            "title": "广告章",
            "content": "为了方便下次阅读，请收藏本站，记录本次阅读位置。",
            "encoding": "utf-8",
        }


class _RepeatedSampleEngine(object):
    def fetch_chapter_content(
        self, source, chapter_url, fallback_title="", max_pages=5
    ):
        del source, chapter_url, max_pages
        return {
            "title": fallback_title,
            "content": "这是被错误重复返回的正文内容。" * 20,
            "encoding": "utf-8",
        }


class SourceDownloaderPhase4Test(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.manager = NovelDownloadManager(self.base_dir, RuntimeConfig())
        self.health_store = SourceHealthStore(self.base_dir / "source_health.json")
        self.registry = _FakeRegistry(
            {
                "source-1": {
                    "source_id": "source-1",
                    "name": "测试源",
                    "source_url": "https://example.com",
                    "supports_download": True,
                    "issues": [],
                }
            },
            {"source-1": {"source_id": "source-1"}},
        )
        self.engine = _FakeEngine()
        self.service = SourceDownloadService(
            self.registry,
            self.engine,
            self.manager,
            SourceDownloadConfig(max_workers=1, sample_chapters=1, sample_min_chars=10),
            source_health_store=self.health_store,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_sample_book_returns_structured_summary(self):
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book",
            "book_name": "测试书",
            "toc": [
                {"index": 0, "title": "第一章", "url": "https://example.com/chapter-1"}
            ],
            "toc_count": 1,
        }

        sample = self.service.sample_book(plan)

        self.assertEqual(sample["sampled_chapter_count"], 1)
        self.assertEqual(sample["sampled_chapters"][0]["title"], "第一章")
        self.assertGreater(sample["sampled_chapters"][0]["content_chars"], 10)

    def test_sample_book_rejects_suspicious_non_novel_content(self):
        service = SourceDownloadService(
            self.registry,
            _BadSampleEngine(),
            self.manager,
            SourceDownloadConfig(max_workers=1, sample_chapters=1, sample_min_chars=1),
            source_health_store=self.health_store,
        )
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book",
            "book_name": "测试书",
            "toc": [
                {"index": 0, "title": "第一章", "url": "https://example.com/chapter-1"}
            ],
        }

        with self.assertRaisesRegex(RuleEngineError, "正文抽样疑似无效"):
            service.sample_book(plan)

    def test_sample_book_rejects_highly_repeated_chapters(self):
        service = SourceDownloadService(
            self.registry,
            _RepeatedSampleEngine(),
            self.manager,
            SourceDownloadConfig(max_workers=1, sample_chapters=3, sample_min_chars=20),
            source_health_store=self.health_store,
        )
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book",
            "book_name": "测试书",
            "toc": [
                {
                    "index": index,
                    "title": "第{index}章".format(index=index + 1),
                    "url": "https://example.com/chapter-{index}".format(index=index),
                }
                for index in range(3)
            ],
        }

        with self.assertRaisesRegex(RuleEngineError, "多个章节内容高度重复"):
            service.sample_book(plan)

    def test_resume_book_job_records_download_health_after_success(self):
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book",
            "toc_url": "https://example.com/book#toc",
            "book_name": "测试书",
            "author": "测试作者",
            "intro": "",
            "toc": [
                {"index": 0, "title": "第一章", "url": "https://example.com/chapter-1"}
            ],
            "toc_count": 1,
        }
        job_info = self.service.create_job_from_plan(plan)

        status = self.service.resume_book_job(job_info["job_id"], auto_assemble=False)
        entry = self.health_store.get_source_health("source-1")

        self.assertEqual(status["state"], "downloaded")
        self.assertEqual(entry["download"]["state"], "healthy")
        self.assertEqual(entry["download"]["completed_chapters"], 1)

    def test_resume_book_job_stops_early_after_repeated_same_errors(self):
        self.engine = _AlwaysForbiddenEngine()
        self.service = SourceDownloadService(
            self.registry,
            self.engine,
            self.manager,
            SourceDownloadConfig(
                max_workers=1,
                sample_chapters=1,
                sample_min_chars=10,
                stop_after_consecutive_failures=6,
                stop_after_same_error=3,
            ),
            source_health_store=self.health_store,
        )
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book",
            "toc_url": "https://example.com/book#toc",
            "book_name": "测试书",
            "author": "测试作者",
            "intro": "",
            "toc": [
                {
                    "index": idx,
                    "title": "第{idx}章".format(idx=idx + 1),
                    "url": "https://example.com/chapter-{idx}".format(idx=idx + 1),
                }
                for idx in range(10)
            ],
            "toc_count": 10,
        }
        job_info = self.service.create_job_from_plan(plan)

        status = self.service.resume_book_job(job_info["job_id"], auto_assemble=False)
        manifest = self.manager.load_manifest(job_info["job_id"])
        replay = self.manager._replay_job(job_info["job_id"])[1]

        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["completed_chapters"], 0)
        self.assertEqual(len(self.engine.chapter_calls), 3)
        self.assertEqual(status["failed_chapters"], 3)
        self.assertIn("停止继续派发新章节", status["state_details"]["stop_reason"])
        self.assertIn("HTTP 403: Forbidden", status["latest_errors"][0]["error"])
        self.assertEqual(replay["last_state"], "failed")
        self.assertEqual(manifest["metadata"]["source_id"], "source-1")
        health = self.health_store.get_source_health("source-1")["download"]
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertEqual(health["last_failure_scope"], "source")
        self.assertEqual(self.health_store.get_active_cooldown("source-1"), {})

    def test_book_specific_download_failure_does_not_trip_source_circuit(self):
        self.service = SourceDownloadService(
            self.registry,
            _BookSpecificFailureEngine(),
            self.manager,
            SourceDownloadConfig(max_workers=1),
            source_health_store=self.health_store,
        )
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book-specific",
            "book_name": "规则异常书籍",
            "toc": [
                {
                    "index": 0,
                    "title": "第一章",
                    "url": "https://example.com/book-specific/chapter-1",
                }
            ],
        }
        job_info = self.service.create_job_from_plan(plan)

        status = self.service.resume_book_job(job_info["job_id"], auto_assemble=False)

        self.assertEqual(status["state"], "failed")
        health = self.health_store.get_source_health("source-1")["download"]
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["last_failure_scope"], "book")
        self.assertEqual(self.health_store.get_active_cooldown("source-1"), {})

    def test_create_job_preserves_chapter_rule_vars_for_resume_download(self):
        self.engine = _RuleContextEngine()
        self.service = SourceDownloadService(
            self.registry,
            self.engine,
            self.manager,
            SourceDownloadConfig(max_workers=1, sample_chapters=1, sample_min_chars=10),
            source_health_store=self.health_store,
        )
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book",
            "toc_url": "https://example.com/book#toc",
            "book_name": "测试书",
            "author": "测试作者",
            "intro": "",
            "toc": [
                {
                    "index": 0,
                    "title": "第一章",
                    "url": "https://example.com/book/chapter-1",
                    "_rule_vars": {"chapter_token": "token-1"},
                }
            ],
            "toc_count": 1,
            "_rule_vars": {"book_token": "book-1"},
        }

        sample = self.service.sample_book(plan)
        job_info = self.service.create_job_from_plan(plan)
        manifest = self.manager.load_manifest(job_info["job_id"])
        status = self.service.resume_book_job(job_info["job_id"], auto_assemble=False)

        self.assertEqual(sample["sampled_chapter_count"], 1)
        self.assertEqual(
            manifest["chapters"][0]["_rule_vars"]["chapter_token"],
            "token-1",
        )
        self.assertEqual(status["state"], "downloaded")
        self.assertEqual(
            self.engine.chapter_calls[-1][2].get("chapter_token"),
            "token-1",
        )

    def test_resume_book_job_rehydrates_missing_rule_vars_for_legacy_manifest(self):
        self.engine = _RuleContextEngine()
        self.service = SourceDownloadService(
            self.registry,
            self.engine,
            self.manager,
            SourceDownloadConfig(max_workers=1, sample_chapters=1, sample_min_chars=10),
            source_health_store=self.health_store,
        )
        legacy_job = self.manager.create_job(
            "测试书",
            [{"title": "第一章", "url": "https://example.com/book/chapter-1"}],
            ExtractionRules(content_regex=r"(?s)(.*)"),
            source_url="https://example.com/book",
            metadata={
                "download_mode": "rule_based",
                "source_id": "source-1",
                "source_name": "测试源",
                "book_url": "https://example.com/book",
                "rule_vars": {"book_token": "book-1"},
            },
        )

        status = self.service.resume_book_job(legacy_job["job_id"], auto_assemble=False)

        self.assertEqual(status["state"], "downloaded")
        self.assertTrue(self.engine.preflight_calls)
        self.assertEqual(
            self.engine.preflight_calls[-1]["rule_context"].get("book_token"),
            "book-1",
        )
        self.assertEqual(
            self.engine.chapter_calls[-1][2].get("chapter_token"),
            "token-1",
        )


if __name__ == "__main__":
    unittest.main()
