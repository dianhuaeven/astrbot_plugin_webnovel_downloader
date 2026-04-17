from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.source_profiles import SourceProfileService
from astrbot_plugin_webnovel_downloader.core.source_registry import SourceRegistry


def _build_html_source(name: str, source_url: str) -> dict[str, object]:
    return {
        "bookSourceName": name,
        "bookSourceUrl": source_url,
        "searchUrl": source_url + "/search?q={{key}}",
        "ruleSearch": {
            "bookList": "//div[@class='book']",
            "name": "./a/text()",
            "bookUrl": "./a/@href",
        },
        "ruleBookInfo": {
            "name": "//h1/text()",
        },
        "ruleToc": {
            "chapterList": "//ul[@id='toc']/li",
            "chapterName": "./a/text()",
            "chapterUrl": "./a/@href",
        },
        "ruleContent": {
            "content": "//div[@id='content']",
        },
    }


def _build_js_source(name: str, source_url: str) -> dict[str, object]:
    payload = _build_html_source(name, source_url)
    payload["enableJs"] = True
    payload["ruleSearch"] = {
        "bookList": "<js> return fetchBooks()",
    }
    return payload


def _build_wordpress_source(name: str, source_url: str) -> dict[str, object]:
    payload = _build_html_source(name, source_url)
    payload["searchUrl"] = source_url + "/search?s={{key}}&post_type=wp-manga"
    payload["ruleToc"] = {
        "chapterList": "li.wp-manga-chapter a",
        "chapterName": "text",
        "chapterUrl": "href",
    }
    payload["ruleContent"] = {
        "content": "div.reading-content",
    }
    return payload


class SourceProfileServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.registry = SourceRegistry(self.base_dir)
        self.service = SourceProfileService(self.registry)

    def tearDown(self):
        self.tempdir.cleanup()

    def _import_source(self, payload: dict[str, object]) -> str:
        result = self.registry.import_sources_from_text(json.dumps([payload], ensure_ascii=False))
        return str(result["sources"][0]["source_id"])

    def test_compile_persists_profile_and_compiled_strategies(self):
        source_id = self._import_source(_build_html_source("示例源", "https://example.com"))

        profile = self.service.compile(source_id)

        self.assertEqual(profile["source_id"], source_id)
        self.assertEqual(profile["template_family"], "generic_html")
        self.assertEqual(profile["preferred_extractors"], ["fallback_rule"])
        self.assertEqual(profile["search_strategy"]["mode"], "keyword_search")
        self.assertEqual(profile["download_strategy"]["mode"], "chapter_list")
        self.assertEqual(profile["search_strategy"]["preferred_extractor"], "fallback_rule")
        self.assertGreater(profile["compiled_at"], 0)
        self.assertEqual(profile["compiled_at"], profile["updated_at"])
        self.assertTrue((self.base_dir / "sources" / "source_profiles.json").exists())

    def test_get_can_compile_missing_profile_on_demand(self):
        source_id = self._import_source(_build_html_source("按需编译源", "https://example.org"))

        self.assertIsNone(self.service.get(source_id))

        profile = self.service.get(source_id, compile_if_missing=True)

        self.assertIsNotNone(profile)
        self.assertEqual(profile["source_id"], source_id)
        self.assertEqual(profile["search_strategy"]["mode"], "keyword_search")
        self.assertEqual(self.service.get(source_id)["source_id"], source_id)

    def test_update_merges_nested_fields_without_recompiling(self):
        source_id = self._import_source(_build_html_source("可更新源", "https://example.net"))
        profile = self.service.compile(source_id)
        time.sleep(0.01)

        updated = self.service.update(
            source_id,
            {
                "template_family": "custom_template",
                "preferred_extractors": ["fallback_rule", "regex", "fallback_rule"],
                "search_strategy": {
                    "fallback": "manual_review",
                },
                "download_strategy": {
                    "notes": {
                        "phase": "3",
                    }
                },
            },
        )

        self.assertEqual(updated["source_id"], source_id)
        self.assertEqual(updated["template_family"], "custom_template")
        self.assertEqual(updated["preferred_extractors"], ["fallback_rule", "regex"])
        self.assertEqual(updated["compiled_at"], profile["compiled_at"])
        self.assertGreater(updated["updated_at"], profile["updated_at"])
        self.assertEqual(updated["search_strategy"]["mode"], "keyword_search")
        self.assertEqual(updated["search_strategy"]["fallback"], "manual_review")
        self.assertEqual(updated["download_strategy"]["notes"]["phase"], "3")

    def test_compile_keeps_registry_unchanged_and_marks_js_profiles(self):
        source_id = self._import_source(_build_js_source("JS源", "https://dynamic.example.com"))
        summary_before = self.registry.get_source_summary(source_id)

        profile = self.service.compile(source_id)
        summary_after = self.registry.get_source_summary(source_id)

        self.assertEqual(summary_before, summary_after)
        self.assertEqual(profile["template_family"], "javascript_dynamic")
        self.assertEqual(profile["preferred_extractors"][0], "javascript_dynamic")
        self.assertEqual(profile["search_strategy"]["mode"], "unsupported_js")
        self.assertEqual(profile["download_strategy"]["mode"], "unsupported_js")

    def test_compile_detects_wordpress_family_and_template_priority(self):
        source_id = self._import_source(
            _build_wordpress_source("WP模板源", "https://wp.example.com")
        )

        profile = self.service.compile(source_id)

        self.assertEqual(profile["template_family"], "wordpress_madara_like")
        self.assertEqual(
            profile["preferred_extractors"][0],
            "template_wordpress_madara_like",
        )
        self.assertEqual(
            profile["search_strategy"]["preferred_extractor"],
            "template_wordpress_madara_like",
        )


if __name__ == "__main__":
    unittest.main()
