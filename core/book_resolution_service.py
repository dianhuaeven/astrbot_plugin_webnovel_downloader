from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from .search_service import SearchService
from .source_health_store import HEALTH_STAGES, SourceHealthStore
from .source_registry import SourceRegistry


@dataclass
class BookResolutionConfig:
    candidate_limit: int = 20


class BookResolutionService:
    def __init__(
        self,
        registry: SourceRegistry,
        search_service: SearchService,
        source_health_store: SourceHealthStore,
        source_profile_service: Any = None,
        config: Optional[BookResolutionConfig] = None,
    ):
        if isinstance(source_profile_service, BookResolutionConfig) and config is None:
            config = source_profile_service
            source_profile_service = None
        self.registry = registry
        self.search_service = search_service
        self.source_health_store = source_health_store
        self.source_profile_service = source_profile_service
        self.config = config or BookResolutionConfig()

    def resolve(
        self,
        keyword: str,
        author: str = "",
        source_ids: Optional[Iterable[str]] = None,
        limit: int = 20,
        include_disabled: bool = False,
    ) -> Dict[str, Any]:
        normalized_keyword = str(keyword or "").strip()
        normalized_author = str(author or "").strip()
        candidate_limit = max(
            1, min(int(limit or 0) or self.config.candidate_limit, 200)
        )
        search_result = self.search_service.search(
            normalized_keyword,
            source_ids,
            candidate_limit,
            include_disabled,
        )
        search_results = list(search_result.get("results") or [])
        health_by_source = self.source_health_store.get_many(
            [
                str(item.get("source_id") or "").strip()
                for item in search_results
                if str(item.get("source_id") or "").strip()
            ]
        )

        seen_keys: set[tuple[str, str, str, str]] = set()
        candidates: list[dict[str, Any]] = []
        skipped_candidates: list[dict[str, Any]] = []

        for item in search_results:
            candidate = self._build_candidate(
                item,
                normalized_keyword,
                normalized_author,
                health_by_source,
            )
            dedupe_key = self._candidate_dedupe_key(candidate)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            if candidate.get("skip_reason"):
                skipped_candidates.append(candidate)
                continue
            candidates.append(candidate)

        candidates.sort(key=self._candidate_sort_key)
        for index, candidate in enumerate(candidates):
            candidate["candidate_index"] = index
        for index, candidate in enumerate(skipped_candidates):
            candidate["candidate_index"] = index
        candidate_groups = self._group_candidates(candidates, skipped_candidates)

        return {
            "keyword": normalized_keyword,
            "author": normalized_author,
            "source_ids": [
                str(item).strip()
                for item in list(source_ids or [])
                if str(item).strip()
            ],
            "include_disabled": bool(include_disabled),
            "limit": candidate_limit,
            "search_result": search_result,
            "candidate_count": len(candidates),
            "skipped_candidate_count": len(skipped_candidates),
            "candidate_group_count": len(candidate_groups),
            "candidates": candidates,
            "skipped_candidates": skipped_candidates,
            "candidate_groups": candidate_groups,
        }

    def resolve_candidates(
        self,
        keyword: str,
        author: str = "",
        source_ids: Optional[Iterable[str]] = None,
        limit: int = 20,
        include_disabled: bool = False,
    ) -> Dict[str, Any]:
        return self.resolve(
            keyword,
            author,
            source_ids,
            limit,
            include_disabled,
        )

    def _build_candidate(
        self,
        item: dict[str, Any],
        keyword: str,
        author: str,
        health_by_source: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        source_id = str(item.get("source_id") or "").strip()
        summary = self._safe_get_source_summary(source_id)
        source_name = str(
            item.get("source_name") or summary.get("name") or source_id
        ).strip()
        title = str(item.get("title") or "").strip()
        candidate_author = str(item.get("author") or "").strip()
        book_url = str(item.get("book_url") or "").strip()
        profile = self._safe_get_source_profile(source_id)
        preferred_extractors = list(profile.get("preferred_extractors") or [])
        health = health_by_source.get(source_id) or {}
        supports_download = self._supports_download_with_runtime(
            item,
            summary,
            health,
        )
        source_issues = [
            str(issue).strip()
            for issue in list(item.get("source_issues") or summary.get("issues") or [])
            if str(issue).strip()
        ]
        candidate = {
            "source_id": source_id,
            "source_name": source_name,
            "title": title,
            "author": candidate_author,
            "book_url": book_url,
            "intro": str(item.get("intro") or "").strip(),
            "kind": str(item.get("kind") or "").strip(),
            "last_chapter": str(item.get("last_chapter") or "").strip(),
            "word_count": str(item.get("word_count") or "").strip(),
            "supports_download": supports_download,
            "source_issues": source_issues[:3],
            "title_match": self._match_title(keyword, title),
            "author_match": self._match_author(author, candidate_author),
            "template_family": str(profile.get("template_family") or "").strip(),
            "preferred_extractor": str(
                preferred_extractors[0] if preferred_extractors else ""
            ).strip(),
            "search_strategy_mode": str(
                (profile.get("search_strategy") or {}).get("mode") or ""
            ).strip(),
            "download_strategy_mode": str(
                (profile.get("download_strategy") or {}).get("mode") or ""
            ).strip(),
            "_rule_vars": dict(item.get("_rule_vars") or {}),
        }
        for stage in HEALTH_STAGES:
            stage_entry = dict(health.get(stage) or {})
            candidate["{stage}_health_state".format(stage=stage)] = str(
                stage_entry.get("state", "unknown") or "unknown"
            )
            candidate["{stage}_health_summary".format(stage=stage)] = (
                self._stage_summary(stage_entry)
            )

        if not book_url:
            candidate["skip_reason"] = "搜索结果缺少 book_url，无法自动下载"
        elif not supports_download:
            candidate["skip_reason"] = self._download_skip_reason(health, source_issues)
        else:
            candidate["skip_reason"] = ""
        return candidate

    def _candidate_dedupe_key(
        self, candidate: dict[str, Any]
    ) -> tuple[str, str, str, str]:
        source_id = str(candidate.get("source_id") or "").strip()
        book_url = str(candidate.get("book_url") or "").strip()
        title = self._normalize_text(candidate.get("title"))
        author = self._normalize_text(candidate.get("author"))
        return (source_id, book_url, title, author)

    def _candidate_sort_key(self, candidate: dict[str, Any]) -> tuple[Any, ...]:
        return (
            self._title_match_rank(candidate.get("title_match")),
            self._author_match_rank(candidate.get("author_match")),
            self._stage_rank(candidate.get("preflight_health_state")),
            self._stage_rank(candidate.get("download_health_state")),
            self._extractor_rank(candidate.get("preferred_extractor")),
            self._stage_rank(candidate.get("search_health_state")),
            len(list(candidate.get("source_issues") or [])),
            self._normalize_text(candidate.get("title")),
            self._normalize_text(candidate.get("author")),
            self._normalize_text(candidate.get("source_name")),
        )

    def _group_candidates(
        self,
        candidates: list[dict[str, Any]],
        skipped_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for item in list(candidates) + list(skipped_candidates):
            title = str(item.get("title") or "").strip()
            author = str(item.get("author") or "").strip()
            title_key = self._normalize_text(title)
            author_key = self._normalize_text(author)
            if not title_key:
                title_key = self._normalize_text(item.get("book_url"))
            key = (title_key, author_key)
            bucket = buckets.setdefault(
                key,
                {
                    "group_id": self._make_group_id(title_key, author_key),
                    "title": title,
                    "author": author,
                    "title_key": title_key,
                    "author_key": author_key,
                    "candidates": [],
                    "skipped_candidates": [],
                },
            )
            if not bucket.get("title") and title:
                bucket["title"] = title
            if not bucket.get("author") and author:
                bucket["author"] = author
            if item.get("skip_reason"):
                bucket["skipped_candidates"].append(dict(item))
            else:
                bucket["candidates"].append(dict(item))

        groups: list[dict[str, Any]] = []
        for bucket in buckets.values():
            bucket["candidates"].sort(key=self._candidate_sort_key)
            bucket["skipped_candidates"].sort(key=self._candidate_sort_key)
            all_items = list(bucket["candidates"]) + list(bucket["skipped_candidates"])
            source_names = []
            seen_sources = set()
            for item in all_items:
                source_id = str(item.get("source_id") or "").strip()
                source_name = str(item.get("source_name") or source_id).strip()
                if source_id and source_id in seen_sources:
                    continue
                if source_id:
                    seen_sources.add(source_id)
                if source_name:
                    source_names.append(source_name)
            best = (
                dict(bucket["candidates"][0])
                if bucket["candidates"]
                else dict(bucket["skipped_candidates"][0])
                if bucket["skipped_candidates"]
                else {}
            )
            group = {
                **bucket,
                "source_count": len(seen_sources) or len(all_items),
                "candidate_count": len(bucket["candidates"]),
                "skipped_candidate_count": len(bucket["skipped_candidates"]),
                "downloadable_source_count": len(bucket["candidates"]),
                "supports_download": bool(bucket["candidates"]),
                "best_source_name": str(best.get("source_name") or "").strip(),
                "best_source_id": str(best.get("source_id") or "").strip(),
                "best_book_url": str(best.get("book_url") or "").strip(),
                "source_names": source_names,
                "best_candidate": best,
            }
            groups.append(group)
        groups.sort(key=self._candidate_group_sort_key)
        for index, group in enumerate(groups):
            group["group_index"] = index
        return groups

    def _candidate_group_sort_key(self, group: dict[str, Any]) -> tuple[Any, ...]:
        best = dict(group.get("best_candidate") or {})
        return (
            self._candidate_sort_key(best),
            -int(group.get("candidate_count", 0) or 0),
            -int(group.get("source_count", 0) or 0),
            self._normalize_text(group.get("title")),
            self._normalize_text(group.get("author")),
        )

    def _make_group_id(self, title_key: str, author_key: str) -> str:
        digest = hashlib.sha1(
            "{title}\n{author}".format(title=title_key, author=author_key).encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        return "book-{digest}".format(digest=digest)

    def _safe_get_source_summary(self, source_id: str) -> dict[str, Any]:
        if not source_id:
            return {}
        try:
            return dict(self.registry.get_source_summary(source_id) or {})
        except Exception:
            return {}

    def _safe_get_source_profile(self, source_id: str) -> dict[str, Any]:
        if self.source_profile_service is None or not source_id:
            return {}
        try:
            return dict(
                self.source_profile_service.get(source_id, compile_if_missing=True)
                or {}
            )
        except Exception:
            return {}

    def _stage_summary(self, stage_entry: dict[str, Any]) -> str:
        note = str(stage_entry.get("note", "") or "").strip()
        if note:
            return note
        error_summary = str(stage_entry.get("last_error_summary", "") or "").strip()
        if error_summary:
            return error_summary
        state = str(stage_entry.get("state", "unknown") or "unknown")
        if state == "healthy":
            return "最近探测成功"
        if state == "degraded":
            return "最近探测不稳定"
        if state == "broken":
            return "最近探测失败"
        if state == "unsupported":
            return "静态规则不支持"
        return "尚无健康记录"

    def _match_title(self, keyword: str, title: str) -> str:
        normalized_keyword = self._normalize_text(keyword)
        normalized_title = self._normalize_text(title)
        if not normalized_keyword:
            return "other"
        if normalized_title == normalized_keyword:
            return "exact"
        if normalized_keyword in normalized_title:
            return "contains"
        return "other"

    def _match_author(self, expected_author: str, actual_author: str) -> str:
        normalized_expected = self._normalize_text(expected_author)
        if not normalized_expected:
            return "unspecified"
        normalized_actual = self._normalize_text(actual_author)
        if not normalized_actual:
            return "missing"
        if normalized_actual == normalized_expected:
            return "exact"
        if (
            normalized_expected in normalized_actual
            or normalized_actual in normalized_expected
        ):
            return "contains"
        return "mismatch"

    def _title_match_rank(self, value: Any) -> int:
        return {"exact": 0, "contains": 1}.get(str(value or ""), 2)

    def _author_match_rank(self, value: Any) -> int:
        return {
            "exact": 0,
            "contains": 1,
            "unspecified": 2,
            "missing": 3,
            "mismatch": 4,
        }.get(str(value or ""), 5)

    def _stage_rank(self, value: Any) -> int:
        return {
            "healthy": 0,
            "degraded": 1,
            "unknown": 2,
            "broken": 3,
            "unsupported": 4,
        }.get(str(value or ""), 5)

    def _extractor_rank(self, value: Any) -> int:
        text = str(value or "").strip()
        if text.startswith("template_"):
            return 0
        if text == "fallback_rule":
            return 1
        if not text:
            return 2
        if "javascript" in text:
            return 4
        return 3

    def _normalize_text(self, value: Any) -> str:
        return str(value or "").strip().casefold()

    def _supports_download_with_runtime(
        self,
        item: dict[str, Any],
        summary: dict[str, Any],
        health: dict[str, dict[str, Any]],
    ) -> bool:
        if not bool(
            item.get("supports_download", summary.get("supports_download", False))
        ):
            return False
        for stage in ("preflight", "download"):
            stage_state = str(
                (health.get(stage) or {}).get("state", "unknown") or "unknown"
            )
            if stage_state == "unsupported":
                return False
        return True

    def _download_skip_reason(
        self,
        health: dict[str, dict[str, Any]],
        source_issues: list[str],
    ) -> str:
        for stage in ("preflight", "download"):
            stage_entry = dict(health.get(stage) or {})
            stage_state = str(stage_entry.get("state", "unknown") or "unknown")
            if stage_state == "unsupported":
                return self._stage_summary(stage_entry)
        return "；".join(source_issues) or "书源当前不支持 TXT 下载"
