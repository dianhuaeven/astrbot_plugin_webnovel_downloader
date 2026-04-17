from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.download_manager import NovelDownloadManager, RuntimeConfig
from astrbot_plugin_webnovel_downloader.core.source_downloader import (
    SourceDownloadConfig,
    SourceDownloadService,
)
from astrbot_plugin_webnovel_downloader.core.source_health_store import SourceHealthStore


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

    def fetch_chapter_content(self, source, chapter_url, fallback_title="", max_pages=5):
        del source, max_pages
        self.chapter_calls.append((chapter_url, fallback_title))
        if "short" in chapter_url:
            return {"title": fallback_title, "content": "太短", "encoding": "utf-8"}
        return {
            "title": fallback_title or "第一章",
            "content": "这里是足够长的测试正文内容，用来通过正文抽样。",
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
            "toc": [{"index": 0, "title": "第一章", "url": "https://example.com/chapter-1"}],
            "toc_count": 1,
        }

        sample = self.service.sample_book(plan)

        self.assertEqual(sample["sampled_chapter_count"], 1)
        self.assertEqual(sample["sampled_chapters"][0]["title"], "第一章")
        self.assertGreater(sample["sampled_chapters"][0]["content_chars"], 10)

    def test_resume_book_job_records_download_health_after_success(self):
        plan = {
            "source_id": "source-1",
            "source_name": "测试源",
            "book_url": "https://example.com/book",
            "toc_url": "https://example.com/book#toc",
            "book_name": "测试书",
            "author": "测试作者",
            "intro": "",
            "toc": [{"index": 0, "title": "第一章", "url": "https://example.com/chapter-1"}],
            "toc_count": 1,
        }
        job_info = self.service.create_job_from_plan(plan)

        status = self.service.resume_book_job(job_info["job_id"], auto_assemble=False)
        entry = self.health_store.get_source_health("source-1")

        self.assertEqual(status["state"], "downloaded")
        self.assertEqual(entry["download"]["state"], "healthy")
        self.assertEqual(entry["download"]["completed_chapters"], 1)


if __name__ == "__main__":
    unittest.main()
