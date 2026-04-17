from __future__ import annotations

from collections.abc import Iterable
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict

from .source_registry import SourceRegistry


SOURCE_PROFILE_SCHEMA_VERSION = 1
_PROFILE_KEYS = {
    "source_id",
    "template_family",
    "preferred_extractors",
    "search_strategy",
    "download_strategy",
    "compiled_at",
    "updated_at",
}


@dataclass(frozen=True)
class SourceProfile:
    source_id: str
    template_family: str
    preferred_extractors: list[str]
    search_strategy: Dict[str, Any]
    download_strategy: Dict[str, Any]
    compiled_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SourceProfileService:
    def __init__(
        self,
        registry: SourceRegistry,
        storage_path: str | Path | None = None,
    ):
        self.registry = registry
        self.storage_path = Path(storage_path or (self.registry.sources_dir / "source_profiles.json"))
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def compile(self, source_id: str) -> Dict[str, Any]:
        with self._lock:
            summary = self.registry.get_source_summary(source_id)
            normalized = self.registry.load_normalized_source(source_id)
            compiled_at = time.time()
            template_family = self._detect_template_family(summary, normalized)
            preferred_extractors = self._detect_preferred_extractors(summary, normalized)
            profile = SourceProfile(
                source_id=source_id,
                template_family=template_family,
                preferred_extractors=preferred_extractors,
                search_strategy=self._build_search_strategy(summary, normalized, preferred_extractors),
                download_strategy=self._build_download_strategy(
                    summary,
                    normalized,
                    preferred_extractors,
                ),
                compiled_at=compiled_at,
                updated_at=compiled_at,
            ).to_dict()
            store = self._load_store()
            store["profiles"][source_id] = profile
            store["updated_at"] = compiled_at
            self._write_store(store)
            return dict(profile)

    def update(self, source_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("profile patch 必须是非空对象")

        with self._lock:
            current = self.get(source_id)
            if current is None:
                current = self.compile(source_id)

            unknown_keys = sorted(set(patch) - (_PROFILE_KEYS - {"source_id", "updated_at"}))
            if unknown_keys:
                raise ValueError(
                    "profile patch 含未知字段: {keys}".format(keys=", ".join(unknown_keys))
                )

            merged = dict(current)
            for key, value in patch.items():
                if key in {"search_strategy", "download_strategy"}:
                    if not isinstance(value, dict):
                        raise ValueError("{key} 必须是对象".format(key=key))
                    merged[key] = self._merge_dicts(merged.get(key) or {}, value)
                    continue
                if key == "preferred_extractors":
                    merged[key] = self._normalize_preferred_extractors(value)
                    continue
                if key == "compiled_at":
                    merged[key] = float(value)
                    continue
                merged[key] = str(value).strip()

            merged["source_id"] = source_id
            merged["updated_at"] = time.time()
            store = self._load_store()
            store["profiles"][source_id] = merged
            store["updated_at"] = merged["updated_at"]
            self._write_store(store)
            return dict(merged)

    def get(self, source_id: str, compile_if_missing: bool = False) -> Dict[str, Any] | None:
        with self._lock:
            store = self._load_store()
            profile = store["profiles"].get(source_id)
            if profile is not None:
                return dict(profile)
            if compile_if_missing:
                return self.compile(source_id)
            return None

    def _load_store(self) -> Dict[str, Any]:
        if not self.storage_path.exists():
            return {
                "schema_version": SOURCE_PROFILE_SCHEMA_VERSION,
                "updated_at": 0.0,
                "profiles": {},
            }

        with open(self.storage_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("source profile 存储损坏：顶层结构不是对象")
        data.setdefault("schema_version", SOURCE_PROFILE_SCHEMA_VERSION)
        data.setdefault("updated_at", 0.0)
        data.setdefault("profiles", {})
        return data

    def _write_store(self, payload: Dict[str, Any]) -> None:
        tmp_path = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.storage_path)

    def _build_search_strategy(
        self,
        summary: Dict[str, Any],
        normalized: Dict[str, Any],
        preferred_extractors: list[str],
    ) -> Dict[str, Any]:
        supports_search = bool(summary.get("supports_search", False))
        mode = "keyword_search"
        if normalized.get("single_url"):
            mode = "single_url"
        elif not supports_search and summary.get("search_uses_js"):
            mode = "unsupported_js"
        elif not supports_search:
            mode = "missing_rules"

        return {
            "mode": mode,
            "supports_search": supports_search,
            "requires_js": bool(summary.get("search_uses_js", False)),
            "search_url": str(normalized.get("search_url") or ""),
            "rule_keys": sorted((normalized.get("rule_search") or {}).keys()),
            "preferred_extractor": preferred_extractors[0],
        }

    def _build_download_strategy(
        self,
        summary: Dict[str, Any],
        normalized: Dict[str, Any],
        preferred_extractors: list[str],
    ) -> Dict[str, Any]:
        supports_download = bool(summary.get("supports_download", False))
        mode = "chapter_list"
        if not supports_download and summary.get("download_uses_js"):
            mode = "unsupported_js"
        elif not supports_download:
            mode = "missing_rules"

        return {
            "mode": mode,
            "supports_download": supports_download,
            "requires_js": bool(summary.get("download_uses_js", False)),
            "rule_book_info_keys": sorted((normalized.get("rule_book_info") or {}).keys()),
            "rule_toc_keys": sorted((normalized.get("rule_toc") or {}).keys()),
            "rule_content_keys": sorted((normalized.get("rule_content") or {}).keys()),
            "preferred_extractor": preferred_extractors[0],
        }

    def _detect_template_family(self, summary: Dict[str, Any], normalized: Dict[str, Any]) -> str:
        source_text = self._collect_rule_text(normalized)
        if normalized.get("single_url"):
            return "single_url"
        if summary.get("search_uses_js") or summary.get("download_uses_js") or normalized.get("enable_js"):
            return "javascript_dynamic"
        if summary.get("has_login_flow"):
            return "authenticated_html"
        if "lnsearchlive" in source_text or ".novel-list .novel-item" in source_text:
            return "novelpub_like"
        if (
            "wp-manga" in source_text
            or "manga_get_chapters" in source_text
            or "reading-content" in source_text
        ):
            return "wordpress_madara_like"
        if (
            "ajax-chapter-option" in source_text
            or "chapter-content" in source_text
            or "chr-content" in source_text
            or "list-chapter" in source_text
        ):
            return "novelfull_like"
        if "@json:" in source_text or "$." in source_text or "jsonpath" in source_text:
            return "json_api"
        return "generic_html"

    def _detect_preferred_extractors(
        self,
        summary: Dict[str, Any],
        normalized: Dict[str, Any],
    ) -> list[str]:
        family = self._detect_template_family(summary, normalized)
        preferred: list[str] = []
        family_to_extractor = {
            "generic_html": "fallback_rule",
            "javascript_dynamic": "javascript_dynamic",
            "wordpress_madara_like": "template_wordpress_madara_like",
            "novelfull_like": "template_novelfull_like",
            "novelpub_like": "template_novelpub_like",
        }
        preferred_extractor = family_to_extractor.get(family)
        if preferred_extractor:
            preferred.append(preferred_extractor)
        preferred.append("fallback_rule")
        return self._normalize_preferred_extractors(preferred)

    def _collect_rule_text(self, normalized: Dict[str, Any]) -> str:
        chunks: list[str] = []
        for key in (
            "search_url",
            "clean_rule_url",
            "rule_search",
            "rule_book_info",
            "rule_toc",
            "rule_content",
        ):
            chunks.extend(self._iter_string_values(normalized.get(key)))
        return "\n".join(chunks).lower()

    def _normalize_preferred_extractors(self, extractors: Any) -> list[str]:
        if isinstance(extractors, str):
            candidates = [extractors]
        elif isinstance(extractors, (list, tuple, set)):
            candidates = list(extractors)
        else:
            raise ValueError("preferred_extractors 必须是字符串或字符串数组")

        normalized: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            value = str(item or "").strip()
            if not value:
                continue
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized or ["fallback_rule"]

    def _merge_dicts(self, base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge_dicts(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _iter_string_values(self, value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value
            return
        if isinstance(value, dict):
            for item in value.values():
                for nested in self._iter_string_values(item):
                    yield nested
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                for nested in self._iter_string_values(item):
                    yield nested
