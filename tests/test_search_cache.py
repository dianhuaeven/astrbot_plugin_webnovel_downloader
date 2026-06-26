from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.search_cache import SearchCacheStore


class SearchCacheStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.store = SearchCacheStore(self.base_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_save_and_load_search_round_trip(self):
        record = self.store.save_search(
            "测试书",
            {
                "searched_sources": 1,
                "successful_sources": 1,
                "results": [
                    {"title": "测试书", "book_url": "https://example.test/book"}
                ],
                "errors": [],
            },
        )

        payload = self.store.load_search(record["search_id"])

        self.assertEqual(payload["record"]["search_id"], record["search_id"])
        self.assertEqual(payload["result"]["results"][0]["title"], "测试书")

    def test_load_search_rejects_path_traversal(self):
        outside_dir = self.base_dir / "sources"
        outside_dir.mkdir()
        (outside_dir / "registry.json").write_text(
            json.dumps(
                {
                    "record": {"search_id": "outside"},
                    "result": {"results": [{"title": "leaked"}]},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            self.store.load_search("../sources/registry")


if __name__ == "__main__":
    unittest.main()
