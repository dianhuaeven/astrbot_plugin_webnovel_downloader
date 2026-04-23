from __future__ import annotations

import unittest

from astrbot_plugin_webnovel_downloader.core.extractors import (
    FallbackRuleExtractor,
    NovelFullLikeExtractor,
    NovelPubLikeExtractor,
    ProfiledExtractor,
    WordpressMadaraLikeExtractor,
)
from astrbot_plugin_webnovel_downloader.core.rule_engine import (
    RuleEngine,
    RuleEngineConfig,
)
from astrbot_plugin_webnovel_downloader.core.session_scraper import ScraperResponse


class _FakeScraper(object):
    def __init__(self, pages):
        self.pages = {
            str(url): str(body).encode("utf-8")
            for url, body in dict(pages or {}).items()
        }
        self.calls = []

    def request(self, url, headers=None, method="GET", body=None, timeout=20.0):
        del headers, body, timeout
        self.calls.append((method, url))
        payload = self.pages.get(url)
        if payload is None:
            raise ValueError("missing page: " + url)
        return ScraperResponse(body=payload, url=url, headers={})


class _FakeProfileService(object):
    def __init__(self, profiles):
        self.profiles = dict(profiles)

    def get(self, source_id, compile_if_missing=False):
        del compile_if_missing
        profile = self.profiles.get(source_id)
        if profile is None:
            return None
        return dict(profile)


class _ExplodingRuleEngine(RuleEngine):
    def __init__(self):
        super().__init__(RuleEngineConfig())
        self.calls = []

    def search_books(self, source, keyword, limit=20):
        self.calls.append(("search", source["source_id"], keyword, limit))
        raise AssertionError(
            "fallback should not be called for profiled template success"
        )

    def build_book_download_plan(self, source, book_url, fallback_title=""):
        self.calls.append(("preflight", source["source_id"], book_url, fallback_title))
        raise AssertionError(
            "fallback should not be called for profiled template success"
        )

    def fetch_chapter_content(
        self, source, chapter_url, fallback_title="", max_pages=5
    ):
        self.calls.append(
            ("content", source["source_id"], chapter_url, fallback_title, max_pages)
        )
        raise AssertionError(
            "fallback should not be called for profiled template success"
        )


class TemplateExtractorTest(unittest.TestCase):
    def test_wordpress_extractor_prefers_ajax_toc(self):
        scraper = _FakeScraper(
            {
                "https://wp.example.com/search?s=测试&post_type=wp-manga&op=&author=&artist=&release=&adult=": """
                    <div class="c-tabs-item__content">
                      <div class="post-title"><h3><a href="/novel/test-book">测试书</a></h3></div>
                    </div>
                """,
                "https://wp.example.com/novel/test-book": """
                    <div class="post-title"><h1>测试书</h1></div>
                    <div class="description-summary">简介</div>
                """,
                "https://wp.example.com/novel/test-book/ajax/chapters/": """
                    <ul><li class="wp-manga-chapter"><a href="/novel/test-book/chapter-1">第一章</a></li></ul>
                """,
                "https://wp.example.com/novel/test-book/chapter-1": """
                    <div class="reading-content"><p>正文一</p><p>正文二</p></div>
                """,
            }
        )
        extractor = WordpressMadaraLikeExtractor(scraper)
        source = {
            "source_id": "wp-source",
            "name": "WP源",
            "source_url": "https://wp.example.com",
            "search_url": "https://wp.example.com/search?s={{key}}&post_type=wp-manga&op=&author=&artist=&release=&adult=",
        }

        results = extractor.search(source, "测试", limit=3)
        plan = extractor.preflight(
            source, "https://wp.example.com/novel/test-book", "测试书"
        )
        chapter = extractor.fetch_content(
            source,
            "https://wp.example.com/novel/test-book/chapter-1",
            "第一章",
        )

        self.assertEqual(results[0]["title"], "测试书")
        self.assertEqual(
            plan["toc_count"] if "toc_count" in plan else len(plan["toc"]), 1
        )
        self.assertEqual(plan["toc"][0]["title"], "第一章")
        self.assertIn(
            ("POST", "https://wp.example.com/novel/test-book/ajax/chapters/"),
            scraper.calls,
        )
        self.assertIn("正文一", chapter["content"])

    def test_profiled_extractor_uses_template_before_fallback(self):
        scraper = _FakeScraper(
            {
                "https://full.example.com/search?keyword=测试": """
                    <div id="list-page">
                      <div class="row"><h3 class="title"><a href="/novel/test-book">测试书</a></h3></div>
                    </div>
                """,
                "https://full.example.com/novel/test-book": """
                    <h3 class="title">测试书</h3>
                    <ul class="list-chapter">
                      <li><a href="/novel/test-book/chapter-1">第一章</a></li>
                    </ul>
                """,
                "https://full.example.com/novel/test-book/chapter-1": """
                    <div id="chapter-content">这里是正文</div>
                """,
            }
        )
        template = NovelFullLikeExtractor(scraper)
        fallback = FallbackRuleExtractor(_ExplodingRuleEngine())
        profiled = ProfiledExtractor(
            fallback_extractor=fallback,
            profile_service=_FakeProfileService(
                {
                    "full-source": {
                        "source_id": "full-source",
                        "template_family": "novelfull_like",
                        "preferred_extractors": [
                            "template_novelfull_like",
                            "fallback_rule",
                        ],
                    }
                }
            ),
            template_extractors={
                "novelfull_like": template,
                "template_novelfull_like": template,
            },
        )
        source = {
            "source_id": "full-source",
            "name": "Full源",
            "source_url": "https://full.example.com",
            "search_url": "https://full.example.com/search?keyword={{key}}",
        }

        results = profiled.search(source, "测试", limit=3)
        plan = profiled.preflight(
            source, "https://full.example.com/novel/test-book", "测试书"
        )
        chapter = profiled.fetch_content(
            source,
            "https://full.example.com/novel/test-book/chapter-1",
            "第一章",
        )

        self.assertEqual(results[0]["title"], "测试书")
        self.assertEqual(plan["toc"][0]["title"], "第一章")
        self.assertIn("这里是正文", chapter["content"])

    def test_novelpub_extractor_fetches_chapters_page(self):
        scraper = _FakeScraper(
            {
                "https://pub.example.com/search?keyword=测试": """
                    <div class="novel-list">
                      <div class="novel-item"><a href="/book/test-book"><span class="novel-title">测试书</span></a></div>
                    </div>
                """,
                "https://pub.example.com/book/test-book": "<h1>测试书</h1>",
                "https://pub.example.com/book/test-book/chapters": """
                    <ul class="chapter-list"><li><a href="/book/test-book/chapter-1">第一章</a></li></ul>
                """,
                "https://pub.example.com/book/test-book/chapter-1": """
                    <div class="chapter-content">章节正文</div>
                """,
            }
        )
        extractor = NovelPubLikeExtractor(scraper)
        source = {
            "source_id": "pub-source",
            "name": "Pub源",
            "source_url": "https://pub.example.com",
            "search_url": "https://pub.example.com/search?keyword={{key}}",
        }

        results = extractor.search(source, "测试", limit=3)
        plan = extractor.preflight(
            source, "https://pub.example.com/book/test-book", "测试书"
        )
        chapter = extractor.fetch_content(
            source,
            "https://pub.example.com/book/test-book/chapter-1",
            "第一章",
        )

        self.assertEqual(results[0]["title"], "测试书")
        self.assertEqual(plan["toc"][0]["title"], "第一章")
        self.assertIn(
            ("GET", "https://pub.example.com/book/test-book/chapters"), scraper.calls
        )
        self.assertIn("章节正文", chapter["content"])

    def test_template_content_cleaner_preserves_block_breaks(self):
        scraper = _FakeScraper(
            {
                "https://pub.example.com/book/test-book/chapter-2": """
                    <div class="chapter-content">
                      <p>第一段</p><p>第二段</p><br/>第三段
                    </div>
                """,
            }
        )
        extractor = NovelPubLikeExtractor(scraper)
        source = {
            "source_id": "pub-source",
            "name": "Pub源",
            "source_url": "https://pub.example.com",
        }

        chapter = extractor.fetch_content(
            source,
            "https://pub.example.com/book/test-book/chapter-2",
            "第二章",
        )

        self.assertEqual(chapter["content"], "第一段\n第二段\n\n第三段")


if __name__ == "__main__":
    unittest.main()
