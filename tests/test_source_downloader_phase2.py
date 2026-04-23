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
    SourceDownloadService,
)


class _FakeRegistry(object):
    def __init__(self, summaries, normalized):
        self._summaries = summaries
        self._normalized = normalized

    def get_source_summary(self, source_id):
        return dict(self._summaries[source_id])

    def load_normalized_source(self, source_id):
        return dict(self._normalized[source_id])


class _FakeEngine(object):
    def __init__(self, plan=None, error=None):
        self.plan = dict(plan or {})
        self.error = error
        self.calls = []

    def build_book_download_plan(self, source, book_url, book_name):
        self.calls.append((source["source_id"], book_url, book_name))
        if self.error is not None:
            raise self.error
        return dict(
            self.plan
            or {
                "book_url": book_url,
                "toc_url": book_url + "#toc",
                "book_name": book_name or "测试书",
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
        )


class SourceDownloaderPhase2Test(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.manager = NovelDownloadManager(self.base_dir, RuntimeConfig())

    def tearDown(self):
        self.tempdir.cleanup()

    def test_preflight_book_rejects_static_unsupported_source(self):
        registry = _FakeRegistry(
            {
                "unsupported": {
                    "source_id": "unsupported",
                    "name": "不可下载源",
                    "supports_download": False,
                    "issues": ["ruleToc 含 JS 规则"],
                }
            },
            {"unsupported": {"source_id": "unsupported"}},
        )
        service = SourceDownloadService(registry, _FakeEngine(), self.manager)

        with self.assertRaisesRegex(ValueError, "不支持 TXT 下载"):
            service.preflight_book(
                "unsupported", "https://example.com/book/1", "测试书"
            )

    def test_preflight_book_returns_rich_plan_summary(self):
        registry = _FakeRegistry(
            {
                "supported": {
                    "source_id": "supported",
                    "name": "可下载源",
                    "source_url": "https://example.com",
                    "supports_download": True,
                    "issues": [],
                }
            },
            {"supported": {"source_id": "supported"}},
        )
        service = SourceDownloadService(registry, _FakeEngine(), self.manager)

        plan = service.preflight_book(
            "supported", "https://example.com/book/1", "测试书"
        )

        self.assertEqual(plan["source_id"], "supported")
        self.assertEqual(plan["source_name"], "可下载源")
        self.assertEqual(plan["book_name"], "测试书")
        self.assertEqual(plan["toc_count"], 1)
        self.assertEqual(plan["toc_url"], "https://example.com/book/1#toc")

    def test_create_job_from_plan_uses_preflight_result(self):
        registry = _FakeRegistry({}, {})
        service = SourceDownloadService(registry, _FakeEngine(), self.manager)
        plan = {
            "source_id": "supported",
            "source_name": "可下载源",
            "book_url": "https://example.com/book/1",
            "toc_url": "https://example.com/book/1#toc",
            "book_name": "测试书",
            "author": "测试作者",
            "intro": "测试简介",
            "toc": [
                {
                    "index": 0,
                    "title": "第一章",
                    "url": "https://example.com/chapter-1",
                }
            ],
            "toc_count": 1,
        }

        job_info = service.create_job_from_plan(plan, "")
        manifest = self.manager.load_manifest(job_info["job_id"])

        self.assertEqual(job_info["source_id"], "supported")
        self.assertEqual(job_info["preflight"]["toc_count"], 1)
        self.assertEqual(manifest["metadata"]["source_id"], "supported")
        self.assertEqual(manifest["metadata"]["source_name"], "可下载源")

    def test_create_book_job_still_wraps_preflight_and_create(self):
        registry = _FakeRegistry(
            {
                "supported": {
                    "source_id": "supported",
                    "name": "可下载源",
                    "source_url": "https://example.com",
                    "supports_download": True,
                    "issues": [],
                }
            },
            {"supported": {"source_id": "supported"}},
        )
        engine = _FakeEngine()
        service = SourceDownloadService(registry, engine, self.manager)

        job_info = service.create_book_job(
            "supported", "https://example.com/book/1", "测试书", ""
        )

        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(job_info["source_name"], "可下载源")
        self.assertEqual(job_info["toc_count"], 1)

    def test_create_job_rejects_existing_output_file(self):
        (self.manager.output_dir / "共享输出.txt").write_text(
            "existing", encoding="utf-8"
        )

        with self.assertRaisesRegex(FileExistsError, "输出文件已存在"):
            self.manager.create_job(
                "第一本书",
                [{"title": "第一章", "url": "https://example.com/1"}],
                ExtractionRules(content_regex=r"(?s)(.*)"),
                output_filename="共享输出",
            )

    def test_create_job_rejects_output_filename_reserved_by_other_job(self):
        first_job = self.manager.create_job(
            "第一本书",
            [{"title": "第一章", "url": "https://example.com/1"}],
            ExtractionRules(content_regex=r"(?s)(.*)"),
            output_filename="共享输出",
        )
        self.assertTrue(first_job["created"])

        with self.assertRaisesRegex(FileExistsError, "已被任务"):
            self.manager.create_job(
                "第二本书",
                [{"title": "第一章", "url": "https://example.com/2"}],
                ExtractionRules(content_regex=r"(?s)(.*)"),
                output_filename="共享输出",
            )


if __name__ == "__main__":
    unittest.main()
