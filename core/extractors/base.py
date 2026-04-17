from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Extractor(ABC):
    """Stable extraction contract for search, preflight, and chapter content."""

    @abstractmethod
    def search(
        self,
        source: dict[str, Any],
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search books from a normalized source."""

    @abstractmethod
    def preflight(
        self,
        source: dict[str, Any],
        book_url: str,
        fallback_title: str = "",
    ) -> dict[str, Any]:
        """Resolve book metadata and chapter list before download."""

    @abstractmethod
    def fetch_content(
        self,
        source: dict[str, Any],
        chapter_url: str,
        fallback_title: str = "",
        max_pages: int = 5,
    ) -> dict[str, str]:
        """Fetch and normalize a single chapter's content."""

    def search_books(
        self,
        source: dict[str, Any],
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Legacy RuleEngine-compatible alias used by current services."""

        return self.search(source, keyword, limit=limit)

    def build_book_download_plan(
        self,
        source: dict[str, Any],
        book_url: str,
        fallback_title: str = "",
    ) -> dict[str, Any]:
        """Legacy RuleEngine-compatible alias used by current services."""

        return self.preflight(source, book_url, fallback_title=fallback_title)

    def fetch_chapter_content(
        self,
        source: dict[str, Any],
        chapter_url: str,
        fallback_title: str = "",
        max_pages: int = 5,
    ) -> dict[str, str]:
        """Legacy RuleEngine-compatible alias used by current services."""

        return self.fetch_content(
            source,
            chapter_url,
            fallback_title=fallback_title,
            max_pages=max_pages,
        )
