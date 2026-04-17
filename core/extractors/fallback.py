from __future__ import annotations

from typing import Any

from ..rule_engine import RuleEngine
from .base import Extractor


class FallbackRuleExtractor(Extractor):
    """Adapter that exposes the new extractor interface over the current RuleEngine."""

    def __init__(self, rule_engine: RuleEngine):
        self.rule_engine = rule_engine

    def __getattr__(self, name: str) -> Any:
        return getattr(self.rule_engine, name)

    def search(
        self,
        source: dict[str, Any],
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.rule_engine.search_books(source, keyword, limit=limit)

    def preflight(
        self,
        source: dict[str, Any],
        book_url: str,
        fallback_title: str = "",
    ) -> dict[str, Any]:
        return self.rule_engine.build_book_download_plan(
            source,
            book_url,
            fallback_title=fallback_title,
        )

    def fetch_content(
        self,
        source: dict[str, Any],
        chapter_url: str,
        fallback_title: str = "",
        max_pages: int = 5,
    ) -> dict[str, str]:
        return self.rule_engine.fetch_chapter_content(
            source,
            chapter_url,
            fallback_title=fallback_title,
            max_pages=max_pages,
        )
