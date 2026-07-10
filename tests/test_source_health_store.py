from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.source_health_store import (
    SourceHealthStore,
)


class SourceHealthStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.store = SourceHealthStore(self.base_dir / "source_health.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_record_success_persists_and_enriches_source(self):
        self.store.record_success(
            "source-a",
            "search",
            elapsed_ms=120.5,
            summary="搜索探测成功",
            metadata={"probe_keyword": "诡秘之主"},
        )

        entry = self.store.get_source_health("source-a")
        self.assertEqual(entry["search"]["state"], "healthy")
        self.assertEqual(entry["search"]["attempts"], 1)
        self.assertEqual(entry["search"]["successes"], 1)
        self.assertEqual(entry["search"]["avg_ms"], 120.5)
        self.assertEqual(entry["search"]["probe_keyword"], "诡秘之主")

        enriched = self.store.enrich_source({"source_id": "source-a", "name": "测试源"})
        self.assertEqual(enriched["search_health_state"], "healthy")
        self.assertEqual(enriched["search_health_summary"], "搜索探测成功")
        self.assertEqual(enriched["preflight_health_state"], "unknown")

    def test_first_source_failure_degrades_and_survives_reload(self):
        self.store.record_failure(
            "source-b",
            "preflight",
            elapsed_ms=88.0,
            error_code="timeout",
            error_summary="目录页超时",
            timeout=True,
        )
        reloaded = SourceHealthStore(self.base_dir / "source_health.json")
        entry = reloaded.get_source_health("source-b")
        self.assertEqual(entry["preflight"]["state"], "degraded")
        self.assertEqual(entry["preflight"]["attempts"], 1)
        self.assertEqual(entry["preflight"]["failures"], 1)
        self.assertEqual(entry["preflight"]["timeouts"], 1)
        self.assertEqual(entry["preflight"]["consecutive_failures"], 1)
        self.assertEqual(entry["preflight"]["cooldown_until"], 0.0)
        self.assertEqual(entry["preflight"]["last_error_code"], "timeout")
        self.assertEqual(entry["preflight"]["last_error_summary"], "目录页超时")

    def test_consecutive_source_failures_enter_cooldown_and_success_resets(self):
        store = SourceHealthStore(
            self.base_dir / "cooldown_health.json",
            failure_threshold=3,
            cooldown_seconds=60.0,
        )
        for _ in range(3):
            entry = store.record_failure(
                "source-cooldown",
                "download",
                error_code="http_403",
                error_summary="HTTP 403: Forbidden",
            )

        self.assertEqual(entry["state"], "broken")
        self.assertEqual(entry["consecutive_failures"], 3)
        self.assertGreater(entry["cooldown_until"], entry["last_failure_at"])
        active = store.get_active_cooldown("source-cooldown")
        self.assertEqual(active["stage"], "download")
        self.assertEqual(active["consecutive_failures"], 3)
        self.assertEqual(
            store.get_active_cooldown(
                "source-cooldown", at=entry["cooldown_until"] + 1.0
            ),
            {},
        )

        recovered = store.record_success("source-cooldown", "download")
        self.assertEqual(recovered["state"], "healthy")
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertEqual(recovered["cooldown_until"], 0.0)

    def test_book_failure_does_not_trip_source_circuit(self):
        for _ in range(4):
            entry = self.store.record_failure(
                "source-book-only",
                "download",
                error_code="content_parse_failed",
                error_summary="当前书籍未解析到正文",
                failure_scope="book",
            )

        self.assertEqual(entry["state"], "degraded")
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["cooldown_until"], 0.0)
        self.assertEqual(entry["last_failure_scope"], "book")
        self.assertEqual(self.store.get_active_cooldown("source-book-only"), {})

    def test_mark_unsupported_and_unknown(self):
        self.store.mark_unsupported(
            "source-c",
            "download",
            summary="静态规则不支持下载",
        )
        self.store.mark_unknown(
            "source-c",
            "preflight",
            summary="尚未自动预检",
        )

        entry = self.store.get_source_health("source-c")
        self.assertEqual(entry["download"]["state"], "unsupported")
        self.assertEqual(entry["download"]["note"], "静态规则不支持下载")
        self.assertEqual(entry["preflight"]["state"], "unknown")
        self.assertEqual(entry["preflight"]["note"], "尚未自动预检")

    def test_store_recreates_schema_when_table_is_missing(self):
        with closing(sqlite3.connect(self.store.sqlite_path)) as connection:
            connection.execute("DROP TABLE IF EXISTS source_stage_health")

        self.store.mark_unsupported(
            "source-d",
            "search",
            summary="重新建表后仍可写入",
        )

        entry = self.store.get_source_health("source-d")
        self.assertEqual(entry["search"]["state"], "unsupported")
        self.assertEqual(entry["search"]["note"], "重新建表后仍可写入")


if __name__ == "__main__":
    unittest.main()
