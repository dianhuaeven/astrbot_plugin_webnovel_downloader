from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.clean_rule_store import CleanRuleRepositoryStore


class CleanRuleStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.store = CleanRuleRepositoryStore(self.base_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_import_same_repo_reuses_stable_repo_id(self):
        payload = (
            '[{"name":"移除广告","pattern":"广告","replacement":"","isRegex":false}]'
        )

        first = self.store.import_rules_from_text(
            payload, "测试仓库", "https://example.com/rules.json"
        )
        second = self.store.import_rules_from_text(
            payload, "测试仓库", "https://example.com/rules.json"
        )

        repositories = self.store.list_repositories()
        self.assertEqual(first["repo_id"], second["repo_id"])
        self.assertEqual(len(repositories), 1)
        self.assertEqual(repositories[0]["repo_id"], first["repo_id"])

    def test_load_applicable_cleaners_deduplicates_same_rule(self):
        payload = (
            '[{"name":"移除广告","pattern":"广告","replacement":"","isRegex":false}]'
        )

        self.store.import_rules_from_text(
            payload, "仓库A", "https://example.com/a.json"
        )
        self.store.import_rules_from_text(
            payload, "仓库B", "https://example.com/b.json"
        )

        cleaners = self.store.load_applicable_cleaners(
            {
                "source_id": "source-1",
                "name": "测试源",
                "source_url": "https://example.com",
                "group": "",
                "clean_rule_url": "",
            }
        )
        self.assertEqual(cleaners, [("广告", "")])


if __name__ == "__main__":
    unittest.main()
