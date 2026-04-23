from __future__ import annotations

from parsel import Selector

from .template_common import SelectorTemplateExtractor


class NovelPubLikeExtractor(SelectorTemplateExtractor):
    template_family = "novelpub_like"
    extractor_id = "template_novelpub_like"

    search_item_selector = ".novel-list .novel-item, #novelListBase .novel-item"
    search_title_selector = ".novel-title, a"
    search_link_selector = "a"
    search_author_selector = ".novel-author, .author"
    search_intro_selector = ".novel-desc, .summary"

    title_selectors = (".novel-title", "h1")
    author_selectors = (".author", ".novel-author")
    intro_selectors = (".summary", ".description")
    toc_link_selectors = ("ul.chapter-list li a",)
    chapter_title_selectors = (".chapter-title", "h1", "title")
    chapter_body_selectors = (".chapter-content",)

    def fetch_toc_selector(self, source, book_url, response, selector):
        del source, response
        chapter_url = book_url.rstrip("/") + "/chapters"
        try:
            chapter_response = self._fetch(chapter_url)
            chapter_selector = Selector(
                text=chapter_response.body.decode("utf-8", errors="replace")
            )
            if chapter_selector.css("ul.chapter-list li a"):
                return chapter_selector, chapter_response.url
        except Exception:
            pass
        return selector, book_url
