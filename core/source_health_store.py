from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable


HEALTH_STAGES = ("search", "preflight", "download")
HEALTH_SCHEMA_VERSION = 1


def _make_stage_entry() -> dict[str, Any]:
    return {
        "state": "unknown",
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "timeouts": 0,
        "avg_ms": 0.0,
        "last_success_at": 0.0,
        "last_failure_at": 0.0,
        "last_error_code": "",
        "last_error_summary": "",
        "note": "",
        "updated_at": 0.0,
    }


def _make_source_entry() -> dict[str, dict[str, Any]]:
    return {stage: _make_stage_entry() for stage in HEALTH_STAGES}


class SourceHealthStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sources = self._load()

    def get_source_health(self, source_id: str) -> dict[str, dict[str, Any]]:
        normalized_source_id = self._normalize_source_id(source_id)
        with self._lock:
            return self._copy_source_entry(
                self._sources.get(normalized_source_id) or _make_source_entry()
            )

    def get_many(self, source_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
        with self._lock:
            if source_ids is None:
                selected = self._sources.items()
            else:
                wanted = [self._normalize_source_id(item) for item in source_ids]
                selected = ((source_id, self._sources.get(source_id)) for source_id in wanted)
            result: dict[str, dict[str, Any]] = {}
            for source_id, entry in selected:
                if not source_id:
                    continue
                result[source_id] = self._copy_source_entry(entry or _make_source_entry())
            return result

    def record_success(
        self,
        source_id: str,
        stage: str,
        elapsed_ms: float = 0.0,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_stage = self._normalize_stage(stage)
        normalized_source_id = self._normalize_source_id(source_id)
        now = time.time()
        with self._lock:
            stage_entry = self._ensure_stage_entry_locked(normalized_source_id, normalized_stage)
            stage_entry["attempts"] = int(stage_entry.get("attempts", 0) or 0) + 1
            stage_entry["successes"] = int(stage_entry.get("successes", 0) or 0) + 1
            stage_entry["avg_ms"] = self._rolling_average(
                float(stage_entry.get("avg_ms", 0.0) or 0.0),
                max(0.0, float(elapsed_ms or 0.0)),
                int(stage_entry["attempts"]),
            )
            stage_entry["state"] = "healthy"
            stage_entry["last_success_at"] = now
            stage_entry["last_error_code"] = ""
            stage_entry["last_error_summary"] = ""
            stage_entry["note"] = str(summary or "").strip()
            stage_entry["updated_at"] = now
            self._merge_metadata(stage_entry, metadata)
            self._save_locked()
            return dict(stage_entry)

    def record_failure(
        self,
        source_id: str,
        stage: str,
        elapsed_ms: float = 0.0,
        error_code: str = "",
        error_summary: str = "",
        timeout: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_stage = self._normalize_stage(stage)
        normalized_source_id = self._normalize_source_id(source_id)
        now = time.time()
        with self._lock:
            stage_entry = self._ensure_stage_entry_locked(normalized_source_id, normalized_stage)
            stage_entry["attempts"] = int(stage_entry.get("attempts", 0) or 0) + 1
            stage_entry["failures"] = int(stage_entry.get("failures", 0) or 0) + 1
            if timeout:
                stage_entry["timeouts"] = int(stage_entry.get("timeouts", 0) or 0) + 1
            stage_entry["avg_ms"] = self._rolling_average(
                float(stage_entry.get("avg_ms", 0.0) or 0.0),
                max(0.0, float(elapsed_ms or 0.0)),
                int(stage_entry["attempts"]),
            )
            successes = int(stage_entry.get("successes", 0) or 0)
            failures = int(stage_entry.get("failures", 0) or 0)
            stage_entry["state"] = "degraded" if successes > 0 and failures <= successes else "broken"
            stage_entry["last_failure_at"] = now
            stage_entry["last_error_code"] = str(error_code or "").strip()
            stage_entry["last_error_summary"] = str(error_summary or "").strip()
            stage_entry["note"] = ""
            stage_entry["updated_at"] = now
            self._merge_metadata(stage_entry, metadata)
            self._save_locked()
            return dict(stage_entry)

    def mark_unsupported(
        self,
        source_id: str,
        stage: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._mark_state(
            source_id,
            stage,
            "unsupported",
            summary=summary,
            metadata=metadata,
        )

    def mark_unknown(
        self,
        source_id: str,
        stage: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._mark_state(
            source_id,
            stage,
            "unknown",
            summary=summary,
            metadata=metadata,
        )

    def enrich_source(self, source: dict[str, Any]) -> dict[str, Any]:
        source_id = self._normalize_source_id(source.get("source_id", ""))
        entry = self.get_source_health(source_id)
        enriched = dict(source)
        for stage in HEALTH_STAGES:
            stage_entry = entry[stage]
            enriched["{stage}_health_state".format(stage=stage)] = stage_entry.get("state", "unknown")
            enriched["{stage}_health_summary".format(stage=stage)] = self._format_stage_summary(
                stage_entry
            )
            enriched["{stage}_health_updated_at".format(stage=stage)] = float(
                stage_entry.get("updated_at", 0.0) or 0.0
            )
        return enriched

    def enrich_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.enrich_source(item) for item in sources]

    def _mark_state(
        self,
        source_id: str,
        stage: str,
        state: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_stage = self._normalize_stage(stage)
        normalized_source_id = self._normalize_source_id(source_id)
        now = time.time()
        with self._lock:
            stage_entry = self._ensure_stage_entry_locked(normalized_source_id, normalized_stage)
            stage_entry["state"] = state
            stage_entry["note"] = str(summary or "").strip()
            if state != "broken":
                stage_entry["last_error_code"] = ""
                stage_entry["last_error_summary"] = ""
            stage_entry["updated_at"] = now
            self._merge_metadata(stage_entry, metadata)
            self._save_locked()
            return dict(stage_entry)

    def _ensure_stage_entry_locked(self, source_id: str, stage: str) -> dict[str, Any]:
        entry = self._sources.setdefault(source_id, _make_source_entry())
        stage_entry = entry.setdefault(stage, _make_stage_entry())
        for key, default in _make_stage_entry().items():
            stage_entry.setdefault(key, default)
        return stage_entry

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        sources = payload.get("sources", payload)
        if not isinstance(sources, dict):
            return {}
        normalized: dict[str, dict[str, dict[str, Any]]] = {}
        for source_id, entry in sources.items():
            if not isinstance(entry, dict):
                continue
            normalized_source_id = self._normalize_source_id(source_id)
            if not normalized_source_id:
                continue
            normalized_entry = _make_source_entry()
            for stage in HEALTH_STAGES:
                stage_entry = entry.get(stage) or {}
                if not isinstance(stage_entry, dict):
                    stage_entry = {}
                merged = _make_stage_entry()
                for key in list(merged):
                    if key in stage_entry:
                        merged[key] = stage_entry[key]
                normalized_entry[stage] = merged
            normalized[normalized_source_id] = normalized_entry
        return normalized

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "updated_at": time.time(),
            "sources": self._sources,
        }
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)

    def _merge_metadata(self, stage_entry: dict[str, Any], metadata: dict[str, Any] | None) -> None:
        if not metadata:
            return
        for key, value in metadata.items():
            if not str(key or "").strip():
                continue
            stage_entry[str(key)] = value

    def _format_stage_summary(self, stage_entry: dict[str, Any]) -> str:
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

    def _copy_source_entry(self, entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            stage: dict(entry.get(stage) or _make_stage_entry())
            for stage in HEALTH_STAGES
        }

    def _normalize_stage(self, stage: str) -> str:
        normalized = str(stage or "").strip().lower()
        if normalized not in HEALTH_STAGES:
            raise ValueError("未知健康阶段: {stage}".format(stage=stage))
        return normalized

    def _normalize_source_id(self, source_id: str) -> str:
        return str(source_id or "").strip()

    def _rolling_average(self, current: float, value: float, sample_count: int) -> float:
        if sample_count <= 1:
            return round(value, 3)
        return round(((current * (sample_count - 1)) + value) / sample_count, 3)
