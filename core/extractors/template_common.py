from __future__ import annotations

import html
import re
from typing import Any, Iterable
from urllib.parse import quote, urljoin

from parsel import Selector

from ..session_scraper import ScraperResponse, SessionScraper
from .base import Extractor


class SelectorTemplateExtractor(Extractor):
    template_family = ""
    extractor_id = ""

    search_item_selector = ""
    search_title_selector = ""
    search_link_selector = ""
    search_author_selector = ""
    search_intro_selector = ""

    title_selectors: tuple[str, ...] = ()
    author_selectors: tuple[str, ...] = ()
    intro_selectors: tuple[str, ...] = ()
    toc_link_selectors: tuple[str, ...] = ()
    chapter_title_selectors: tuple[str, ...] = ()
    chapter_body_selectors: tuple[str, ...] = ()

    def __init__(self, scraper: SessionScraper):
        self.scraper = scraper

    def search(
        self,
        source: dict[str, Any],
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        url = self.build_search_url(source, keyword)
        response = self._fetch(url)
        selector = Selector(text=response.body.decode("utf-8", errors="replace"))
        items = self._select_nodes(selector, (self.search_item_selector,))
        results: list[dict[str, Any]] = []
        for item in items:
            title = self._first_text(item, (self.search_title_selector, self.search_link_selector))
            book_url = self._first_attr(item, (self.search_link_selector,), "href")
            if not title or not book_url:
                continue
            results.append(
                {
                    "source_id": str(source.get("source_id") or "").strip(),
                    "source_name": str(source.get("name") or "").strip(),
                    "title": title,
                    "author": self._first_text(item, (self.search_author_selector,)),
                    "book_url": urljoin(response.url, book_url),
                    "intro": self._first_text(item, (self.search_intro_selector,)),
                    "kind": "",
                    "last_chapter": "",
                    "word_count": "",
                    "cover_url": "",
                }
            )
            if len(results) >= max(1, int(limit)):
                break
        if not results:
            raise ValueError(
                "{name} 未命中模板搜索结果结构".format(
                    name=self.extractor_id or self.template_family or self.__class__.__name__
                )
            )
        return results

    def preflight(
        self,
        source: dict[str, Any],
        book_url: str,
        fallback_title: str = "",
    ) -> dict[str, Any]:
        response = self._fetch(book_url)
        selector = Selector(text=response.body.decode("utf-8", errors="replace"))
        title = self._first_text(selector, self.title_selectors) or fallback_title
        author = self._first_text(selector, self.author_selectors)
        intro = self._first_text(selector, self.intro_selectors)
        toc_selector, toc_url = self.fetch_toc_selector(source, book_url, response, selector)
        toc_links = self._select_nodes(toc_selector, self.toc_link_selectors)
        toc: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for link in toc_links:
            chapter_title = self._node_text(link)
            chapter_url = self._first_attr(link, ("::self",), "href")
            if not chapter_title or not chapter_url:
                continue
            absolute_url = urljoin(toc_url, chapter_url)
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            toc.append(
                {
                    "index": len(toc),
                    "title": chapter_title,
                    "url": absolute_url,
                }
            )
        if not toc:
            raise ValueError(
                "{name} 未命中模板目录结构".format(
                    name=self.extractor_id or self.template_family or self.__class__.__name__
                )
            )
        return {
            "book_url": response.url,
            "toc_url": toc_url,
            "book_name": title or fallback_title or "未命名小说",
            "author": author,
            "intro": intro,
            "toc": toc,
        }

    def fetch_content(
        self,
        source: dict[str, Any],
        chapter_url: str,
        fallback_title: str = "",
        max_pages: int = 5,
    ) -> dict[str, str]:
        del max_pages
        response = self._fetch(chapter_url)
        selector = Selector(text=response.body.decode("utf-8", errors="replace"))
        title = self._first_text(selector, self.chapter_title_selectors) or fallback_title
        body_node = self._first_node(selector, self.chapter_body_selectors)
        if body_node is None:
            raise ValueError(
                "{name} 未命中模板正文字段".format(
                    name=self.extractor_id or self.template_family or self.__class__.__name__
                )
            )
        content = self._clean_html_text(body_node.get())
        if not content:
            raise ValueError(
                "{name} 命中模板正文节点但解析结果为空".format(
                    name=self.extractor_id or self.template_family or self.__class__.__name__
                )
            )
        return {
            "title": title or fallback_title or "",
            "content": content,
            "encoding": "utf-8",
        }

    def build_search_url(self, source: dict[str, Any], keyword: str) -> str:
        raw_url = str(source.get("search_url") or "").strip()
        if not raw_url:
            base_url = str(source.get("source_url") or "").rstrip("/")
            if not base_url:
                raise ValueError("书源缺少 search_url/source_url，无法走模板搜索")
            return "{base}/search?keyword={keyword}".format(
                base=base_url,
                keyword=quote(keyword),
            )
        return (
            raw_url.replace("{{key}}", keyword)
            .replace("{{keyword}}", keyword)
            .replace("{{keyEncoded}}", quote(keyword))
            .replace("{{key_encoded}}", quote(keyword))
        )

    def fetch_toc_selector(
        self,
        source: dict[str, Any],
        book_url: str,
        response: ScraperResponse,
        selector: Selector,
    ) -> tuple[Selector, str]:
        del source, book_url
        return selector, response.url

    def _fetch(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, Any] | None = None,
    ) -> ScraperResponse:
        return self.scraper.request(url, headers=headers, method=method, body=body, timeout=20.0)

    def _select_nodes(self, selector: Selector, selectors: Iterable[str]) -> list[Selector]:
        for expression in selectors:
            expression = str(expression or "").strip()
            if not expression:
                continue
            if expression == "::self":
                return [selector]
            if expression.startswith("/"):
                nodes = selector.xpath(expression)
            else:
                nodes = selector.css(expression)
            if nodes:
                return list(nodes)
        return []

    def _first_node(self, selector: Selector, selectors: Iterable[str]) -> Selector | None:
        nodes = self._select_nodes(selector, selectors)
        if nodes:
            return nodes[0]
        return None

    def _first_text(self, selector: Selector, selectors: Iterable[str]) -> str:
        node = self._first_node(selector, selectors)
        if node is None:
            return ""
        return self._node_text(node)

    def _first_attr(self, selector: Selector, selectors: Iterable[str], attr: str) -> str:
        node = self._first_node(selector, selectors)
        if node is None:
            return ""
        if attr == "href" and str(node.root.tag).lower() == "a":
            value = node.attrib.get("href")
        else:
            value = node.attrib.get(attr)
        return str(value or "").strip()

    def _node_text(self, selector: Selector) -> str:
        text = selector.xpath("normalize-space(string(.))").get()
        return str(text or "").strip()

    def _clean_html_text(self, value: str) -> str:
        text = str(value or "")
        text = re.sub(r"(?is)<script.*?</script>", "", text)
        text = re.sub(r"(?is)<style.*?</style>", "", text)
        text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p\\s*>", "\n", text)
        text = re.sub(r"(?i)</div\\s*>", "\n", text)
        text = re.sub(r"(?is)<[^>]+>", "", text)
        text = html.unescape(text)
        text = text.replace("\u00a0", " ").replace("\u3000", "  ")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()
