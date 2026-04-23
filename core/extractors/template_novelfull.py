from __future__ import annotations

import re

from parsel import Selector

from .template_common import SelectorTemplateExtractor


class NovelFullLikeExtractor(SelectorTemplateExtractor):
    template_family = "novelfull_like"
    extractor_id = "template_novelfull_like"

    search_item_selector = "#list-page .row, .list.list-truyen .row, .truyen-list .row"
    search_title_selector = "h3[class*='title'] > a, h3.title > a, a"
    search_link_selector = "h3[class*='title'] > a, h3.title > a, a"
    search_intro_selector = "div.desc-text, .info"

    title_selectors = ("h3.title", "h1.title", ".desc h3", "h1")
    author_selectors = ("a[href*='author']", ".info a", ".author")
    intro_selectors = (".desc-text", ".desc")
    toc_link_selectors = ("ul.list-chapter > li > a[href]", "select > option[value]")
    chapter_title_selectors = ("a.chapter-title", "h3.chapter-title", "h1", "title")
    chapter_body_selectors = ("#chr-content", "#chapter-content")

    def fetch_toc_selector(self, source, book_url, response, selector):
        links = selector.css("ul.list-chapter > li > a[href], select > option[value]")
        if links:
            return selector, response.url

        novel_id = self._extract_novel_id(
            response.body.decode("utf-8", errors="replace")
        )
        if not novel_id:
            return selector, response.url

        base_url = str(source.get("source_url") or "").rstrip("/")
        ajax_url = "{base}/ajax-chapter-option?novelId={novel_id}".format(
            base=base_url,
            novel_id=novel_id,
        )
        try:
            ajax_response = self._fetch(ajax_url)
            ajax_selector = Selector(
                text=ajax_response.body.decode("utf-8", errors="replace")
            )
            if ajax_selector.css(
                "ul.list-chapter > li > a[href], select > option[value]"
            ):
                return ajax_selector, ajax_response.url
        except Exception:
            pass
        return selector, response.url

    def _extract_novel_id(self, html_text: str) -> str:
        patterns = (
            r"novelId\s*[:=]\s*[\"']?(?P<id>\d+)",
            r"data-novel-id=[\"'](?P<id>\d+)[\"']",
            r"id_novel[\"']?\s*value=[\"'](?P<id>\d+)[\"']",
        )
        for pattern in patterns:
            match = re.search(pattern, html_text, flags=re.I)
            if match:
                return str(match.group("id") or "").strip()
        return ""
