from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .source_models import (
    REGISTRY_SCHEMA_VERSION,
    build_source_summary,
    normalize_book_source,
    parse_source_payload,
)


class SourceRegistry:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.sources_dir = self.base_dir / "sources"
        self.raw_dir = self.sources_dir / "raw"
        self.normalized_dir = self.sources_dir / "normalized"
        self.registry_path = self.sources_dir / "registry.json"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.normalized_dir.mkdir(parents=True, exist_ok=True)

    def import_sources_from_text(self, raw_text: str) -> Dict[str, Any]:
        payload = parse_source_payload(raw_text)
        registry = self._load_registry()
        imported: List[Dict[str, Any]] = []
        warnings: List[str] = []

        for raw_source in payload:
            normalized = normalize_book_source(raw_source)
            source_id = normalized["source_id"]
            updated_at = time.time()

            self._write_json(
                self.raw_dir / "{source_id}.json".format(source_id=source_id),
                raw_source,
            )
            self._write_json(
                self.normalized_dir / "{source_id}.json".format(source_id=source_id),
                normalized,
            )

            summary = build_source_summary(normalized, updated_at).to_dict()
            registry["sources"][source_id] = summary
            imported.append(summary)
            if summary.get("issues"):
                warnings.append(
                    "{name}: {issues}".format(
                        name=summary.get("name", source_id),
                        issues="；".join(summary.get("issues", [])),
                    )
                )

        registry["updated_at"] = time.time()
        self._write_json(self.registry_path, registry)
        return {
            "imported_count": len(imported),
            "supported_search_count": sum(
                1 for item in imported if item.get("supports_search")
            ),
            "supported_download_count": sum(
                1 for item in imported if item.get("supports_download")
            ),
            "warnings": warnings,
            "sources": imported,
        }

    def list_sources(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        sources = sorted(
            registry["sources"].values(),
            key=lambda item: (item.get("enabled") is not True, item.get("name", "")),
        )
        if enabled_only:
            sources = [item for item in sources if item.get("enabled")]
        return sources

    def load_enabled_source_summaries(
        self,
        source_ids: Optional[Iterable[str]] = None,
        include_disabled: bool = False,
    ) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        selected_ids = set(source_ids or [])
        result: List[Dict[str, Any]] = []
        for source_id, summary in registry["sources"].items():
            if selected_ids and source_id not in selected_ids:
                continue
            if not include_disabled and not summary.get("enabled", False):
                continue
            result.append(summary)
        return result

    def get_source_summary(self, source_id: str) -> Dict[str, Any]:
        registry = self._load_registry()
        try:
            return registry["sources"][source_id]
        except KeyError as exc:
            raise ValueError(
                "未找到书源 {source_id}".format(source_id=source_id)
            ) from exc

    def load_normalized_source(self, source_id: str) -> Dict[str, Any]:
        path = self.normalized_dir / "{source_id}.json".format(source_id=source_id)
        if not path.exists():
            raise ValueError("未找到书源 {source_id}".format(source_id=source_id))
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def load_enabled_sources(
        self,
        source_ids: Optional[Iterable[str]] = None,
        include_disabled: bool = False,
    ) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        selected_ids = set(source_ids or [])
        result: List[Dict[str, Any]] = []
        for source_id, summary in registry["sources"].items():
            if selected_ids and source_id not in selected_ids:
                continue
            if not include_disabled and not summary.get("enabled", False):
                continue
            result.append(self.load_normalized_source(source_id))
        return result

    def set_enabled(self, source_id: str, enabled: bool) -> Dict[str, Any]:
        registry = self._load_registry()
        if source_id not in registry["sources"]:
            raise ValueError("未找到书源 {source_id}".format(source_id=source_id))
        registry["sources"][source_id]["enabled"] = bool(enabled)
        registry["sources"][source_id]["updated_at"] = time.time()
        self._write_json(self.registry_path, registry)

        normalized = self.load_normalized_source(source_id)
        normalized["enabled"] = bool(enabled)
        normalized["last_imported_at"] = time.time()
        self._write_json(
            self.normalized_dir / "{source_id}.json".format(source_id=source_id),
            normalized,
        )
        return registry["sources"][source_id]

    def remove_source(self, source_id: str) -> Dict[str, Any]:
        registry = self._load_registry()
        if source_id not in registry["sources"]:
            raise ValueError("未找到书源 {source_id}".format(source_id=source_id))
        removed = registry["sources"].pop(source_id)
        registry["updated_at"] = time.time()
        self._write_json(self.registry_path, registry)

        for directory in (self.raw_dir, self.normalized_dir):
            path = directory / "{source_id}.json".format(source_id=source_id)
            if path.exists():
                path.unlink()
        return removed

    def _load_registry(self) -> Dict[str, Any]:
        if not self.registry_path.exists():
            return {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "updated_at": 0,
                "sources": {},
            }
        with open(self.registry_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("书源注册表损坏：顶层结构不是对象")
        data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION)
        data.setdefault("updated_at", 0)
        data.setdefault("sources", {})
        return data

    def _write_json(self, path: Path, payload: Any) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
