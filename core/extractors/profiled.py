from __future__ import annotations

from typing import Any

from .base import Extractor


class ProfiledExtractor(Extractor):
    def __init__(
        self,
        fallback_extractor: Extractor,
        profile_service: Any | None = None,
        template_extractors: dict[str, Extractor] | None = None,
    ):
        self.fallback_extractor = fallback_extractor
        self.profile_service = profile_service
        self.template_extractors = dict(template_extractors or {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self.fallback_extractor, name)

    def search(
        self,
        source: dict[str, Any],
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for extractor in self._resolve_extractors(source):
            try:
                results = extractor.search(source, keyword, limit=limit)
                if results or extractor is self.fallback_extractor:
                    return results
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return []

    def preflight(
        self,
        source: dict[str, Any],
        book_url: str,
        fallback_title: str = "",
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for extractor in self._resolve_extractors(source):
            try:
                return extractor.preflight(
                    source, book_url, fallback_title=fallback_title
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ValueError("没有可用提取器")

    def fetch_content(
        self,
        source: dict[str, Any],
        chapter_url: str,
        fallback_title: str = "",
        max_pages: int = 5,
    ) -> dict[str, str]:
        last_error: Exception | None = None
        for extractor in self._resolve_extractors(source):
            try:
                return extractor.fetch_content(
                    source,
                    chapter_url,
                    fallback_title=fallback_title,
                    max_pages=max_pages,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ValueError("没有可用提取器")

    def _resolve_extractors(self, source: dict[str, Any]) -> list[Extractor]:
        ordered: list[Extractor] = []
        profile = None
        source_id = str(source.get("source_id") or "").strip()
        if self.profile_service is not None and source_id:
            profile = self.profile_service.get(source_id, compile_if_missing=True)
        for extractor_id in list((profile or {}).get("preferred_extractors") or []):
            extractor = self.template_extractors.get(str(extractor_id))
            if extractor is not None and extractor not in ordered:
                ordered.append(extractor)
        family = str((profile or {}).get("template_family") or "").strip()
        if family:
            extractor = self.template_extractors.get(family)
            if extractor is not None and extractor not in ordered:
                ordered.append(extractor)
        if self.fallback_extractor not in ordered:
            ordered.append(self.fallback_extractor)
        return ordered
