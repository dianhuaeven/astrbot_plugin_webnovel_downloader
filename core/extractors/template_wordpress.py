from __future__ import annotations

from urllib.parse import urljoin

from parsel import Selector

from .template_common import SelectorTemplateExtractor


class WordpressMadaraLikeExtractor(SelectorTemplateExtractor):
    template_family = "wordpress_madara_like"
    extractor_id = "template_wordpress_madara_like"

    search_item_selector = ".c-tabs-item__content, .page-item-detail, .post-title"
    search_title_selector = ".post-title h3 a, .post-title h4 a, a"
    search_link_selector = ".post-title h3 a, .post-title h4 a, a"
    search_author_selector = ".author-content a, .mg_author a"
    search_intro_selector = ".tab-summary .summary__content, .description-summary"

    title_selectors = (".post-title h1",)
    author_selectors = ('.author-content a[href*="manga-author"]', ".author-content a")
    intro_selectors = (".description-summary", ".summary__content")
    toc_link_selectors = ("li.wp-manga-chapter a",)
    chapter_title_selectors = (".chapter-title", "h1", "title")
    chapter_body_selectors = ("div.reading-content",)

    def fetch_toc_selector(self, source, book_url, response, selector):
        del book_url
        ajax_url = response.url.split("?")[0].rstrip("/") + "/ajax/chapters/"
        try:
            ajax_response = self._fetch(ajax_url, method="POST")
            ajax_selector = Selector(text=ajax_response.body.decode("utf-8", errors="replace"))
            if ajax_selector.css("li.wp-manga-chapter a"):
                return ajax_selector, ajax_response.url
        except Exception:
            pass
        return super().fetch_toc_selector(source, response.url, response, selector)
