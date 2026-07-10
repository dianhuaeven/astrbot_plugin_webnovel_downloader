from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.download_manager import (
    ExtractionRules,
    NovelDownloadManager,
    RuntimeConfig,
)


class DownloadSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.manager = NovelDownloadManager(Path(self.tempdir.name), RuntimeConfig())

    def test_job_id_rejects_traversal_absolute_and_separator_inputs(self):
        invalid_ids = (
            "../secret",
            "..\\secret",
            "job/secret",
            "job\\secret",
            "job?secret",
            "C:\\Windows\\secret",
            "/absolute/path",
            "job.",
        )
        for job_id in invalid_ids:
            with self.subTest(job_id=job_id):
                with self.assertRaises(ValueError):
                    self.manager.get_status(job_id)

    def test_manifest_ownership_filters_cross_user_queries(self):
        job = self.manager.create_job(
            "所有权测试书",
            [{"title": "第一章", "url": "https://example.com/1"}],
            ExtractionRules(content_regex=r"(?s)(.*)"),
            requester_id="user-a",
            session_id="session-a",
        )
        manifest = self.manager.load_manifest(job["job_id"])
        self.assertEqual(manifest["requester_id"], "user-a")
        self.assertEqual(manifest["session_id"], "session-a")
        self.assertEqual(
            self.manager.get_status_for(job["job_id"], "user-a", "session-other")[
                "job_id"
            ],
            job["job_id"],
        )
        with self.assertRaises(PermissionError):
            self.manager.get_status_for(job["job_id"], "user-b", "session-b")
        self.assertEqual(self.manager.list_jobs_for("user-b", "session-b"), [])
        self.assertEqual(len(self.manager.list_jobs_for("admin", "", True)), 1)

    def test_existing_deterministic_job_cannot_be_claimed_by_another_user(self):
        toc = [{"title": "第一章", "url": "https://example.com/1"}]
        self.manager.create_job(
            "复用测试书",
            toc,
            ExtractionRules(content_regex=r"(?s)(.*)"),
            requester_id="user-a",
            session_id="session-a",
        )
        with self.assertRaises(PermissionError):
            self.manager.create_job(
                "复用测试书",
                toc,
                ExtractionRules(content_regex=r"(?s)(.*)"),
                requester_id="user-b",
                session_id="session-b",
            )


if __name__ == "__main__":
    unittest.main()
