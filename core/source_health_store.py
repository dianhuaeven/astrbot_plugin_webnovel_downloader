from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .sqlite_support import connect_sqlite, derive_sqlite_path


HEALTH_STAGES = ("search", "preflight", "download")
HEALTH_SCHEMA_VERSION = 2
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 15 * 60.0


def _make_stage_entry() -> dict[str, Any]:
    return {
        "state": "unknown",
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "timeouts": 0,
        "consecutive_failures": 0,
        "cooldown_until": 0.0,
        "last_failure_scope": "",
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
    def __init__(
        self,
        path: str | Path,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ):
        self.path = Path(path)
        self.sqlite_path = derive_sqlite_path(self.path)
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._lock = threading.RLock()
        self._initialize()
        self._migrate_json_if_needed()

    def get_source_health(self, source_id: str) -> dict[str, dict[str, Any]]:
        normalized_source_id = self._normalize_source_id(source_id)
        if not normalized_source_id:
            return _make_source_entry()
        return self.get_many([normalized_source_id]).get(
            normalized_source_id, _make_source_entry()
        )

    def get_many(
        self, source_ids: Iterable[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = self._normalize_ids(source_ids)
        with self._lock:
            with connect_sqlite(self.sqlite_path) as connection:
                # Ensure the table exists on the same connection that will issue the query.
                self._ensure_schema(connection)
                if normalized_ids is None:
                    rows = connection.execute(
                        """
                        SELECT
                            source_id,
                            stage,
                            state,
                            attempts,
                            successes,
                            failures,
                            timeouts,
                            consecutive_failures,
                            cooldown_until,
                            last_failure_scope,
                            avg_ms,
                            last_success_at,
                            last_failure_at,
                            last_error_code,
                            last_error_summary,
                            note,
                            updated_at,
                            metadata_json
                        FROM source_stage_health
                        """
                    ).fetchall()
                    result: dict[str, dict[str, dict[str, Any]]] = {}
                elif not normalized_ids:
                    return {}
                else:
                    placeholders = ",".join("?" for _ in normalized_ids)
                    rows = connection.execute(
                        """
                        SELECT
                            source_id,
                            stage,
                            state,
                            attempts,
                            successes,
                            failures,
                            timeouts,
                            consecutive_failures,
                            cooldown_until,
                            last_failure_scope,
                            avg_ms,
                            last_success_at,
                            last_failure_at,
                            last_error_code,
                            last_error_summary,
                            note,
                            updated_at,
                            metadata_json
                        FROM source_stage_health
                        WHERE source_id IN ({placeholders})
                        """.format(placeholders=placeholders),
                        normalized_ids,
                    ).fetchall()
                    result = {
                        source_id: _make_source_entry() for source_id in normalized_ids
                    }
        for row in rows:
            source_id = str(row["source_id"] or "").strip()
            stage = str(row["stage"] or "").strip()
            if not source_id or stage not in HEALTH_STAGES:
                continue
            result.setdefault(source_id, _make_source_entry())[stage] = (
                self._row_to_stage_entry(row)
            )
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
            source_entry = self.get_source_health(normalized_source_id)
            stage_entry = dict(
                source_entry.get(normalized_stage) or _make_stage_entry()
            )
            stage_entry["attempts"] = int(stage_entry.get("attempts", 0) or 0) + 1
            stage_entry["successes"] = int(stage_entry.get("successes", 0) or 0) + 1
            stage_entry["avg_ms"] = self._rolling_average(
                float(stage_entry.get("avg_ms", 0.0) or 0.0),
                max(0.0, float(elapsed_ms or 0.0)),
                int(stage_entry["attempts"]),
            )
            stage_entry["state"] = "healthy"
            stage_entry["consecutive_failures"] = 0
            stage_entry["cooldown_until"] = 0.0
            stage_entry["last_failure_scope"] = ""
            stage_entry["last_success_at"] = now
            stage_entry["last_error_code"] = ""
            stage_entry["last_error_summary"] = ""
            stage_entry["note"] = str(summary or "").strip()
            stage_entry["updated_at"] = now
            self._merge_metadata(stage_entry, metadata)
            source_entry[normalized_stage] = stage_entry
            self._write_source_entry(
                source_id=normalized_source_id, source_entry=source_entry
            )
            return dict(stage_entry)

    def record_failure(
        self,
        source_id: str,
        stage: str,
        elapsed_ms: float = 0.0,
        error_code: str = "",
        error_summary: str = "",
        timeout: bool = False,
        failure_scope: str = "source",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_stage = self._normalize_stage(stage)
        normalized_source_id = self._normalize_source_id(source_id)
        now = time.time()
        with self._lock:
            source_entry = self.get_source_health(normalized_source_id)
            stage_entry = dict(
                source_entry.get(normalized_stage) or _make_stage_entry()
            )
            stage_entry["attempts"] = int(stage_entry.get("attempts", 0) or 0) + 1
            stage_entry["failures"] = int(stage_entry.get("failures", 0) or 0) + 1
            if timeout:
                stage_entry["timeouts"] = int(stage_entry.get("timeouts", 0) or 0) + 1
            stage_entry["avg_ms"] = self._rolling_average(
                float(stage_entry.get("avg_ms", 0.0) or 0.0),
                max(0.0, float(elapsed_ms or 0.0)),
                int(stage_entry["attempts"]),
            )
            normalized_scope = str(failure_scope or "source").strip().lower()
            if normalized_scope not in {"source", "book", "chapter"}:
                raise ValueError("failure_scope 仅支持 source/book/chapter")
            if normalized_scope == "source":
                consecutive_failures = (
                    int(stage_entry.get("consecutive_failures", 0) or 0) + 1
                )
                stage_entry["consecutive_failures"] = consecutive_failures
                if consecutive_failures >= self.failure_threshold:
                    stage_entry["state"] = "broken"
                    stage_entry["cooldown_until"] = now + self.cooldown_seconds
                else:
                    stage_entry["state"] = "degraded"
                    stage_entry["cooldown_until"] = 0.0
            else:
                if str(stage_entry.get("state") or "unknown") not in {
                    "broken",
                    "unsupported",
                }:
                    stage_entry["state"] = "degraded"
            stage_entry["last_failure_scope"] = normalized_scope
            stage_entry["last_failure_at"] = now
            stage_entry["last_error_code"] = str(error_code or "").strip()
            stage_entry["last_error_summary"] = str(error_summary or "").strip()
            stage_entry["note"] = ""
            stage_entry["updated_at"] = now
            self._merge_metadata(stage_entry, metadata)
            source_entry[normalized_stage] = stage_entry
            self._write_source_entry(
                source_id=normalized_source_id, source_entry=source_entry
            )
            return dict(stage_entry)

    def get_active_cooldown(
        self,
        source_id: str,
        stages: Iterable[str] = ("preflight", "download"),
        at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if at is None else float(at)
        health = self.get_source_health(source_id)
        active: dict[str, Any] = {}
        for stage in stages:
            normalized_stage = self._normalize_stage(stage)
            entry = dict(health.get(normalized_stage) or {})
            cooldown_until = float(entry.get("cooldown_until", 0.0) or 0.0)
            if cooldown_until <= now:
                continue
            if not active or cooldown_until > float(
                active.get("cooldown_until", 0.0) or 0.0
            ):
                active = {
                    "source_id": self._normalize_source_id(source_id),
                    "stage": normalized_stage,
                    "cooldown_until": cooldown_until,
                    "remaining_seconds": max(0.0, cooldown_until - now),
                    "consecutive_failures": int(
                        entry.get("consecutive_failures", 0) or 0
                    ),
                    "error": str(entry.get("last_error_summary") or "").strip(),
                }
        return active

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
            enriched["{stage}_health_state".format(stage=stage)] = stage_entry.get(
                "state", "unknown"
            )
            enriched["{stage}_health_summary".format(stage=stage)] = (
                self._format_stage_summary(stage_entry)
            )
            enriched["{stage}_health_updated_at".format(stage=stage)] = float(
                stage_entry.get("updated_at", 0.0) or 0.0
            )
            enriched["{stage}_health_consecutive_failures".format(stage=stage)] = int(
                stage_entry.get("consecutive_failures", 0) or 0
            )
            enriched["{stage}_health_cooldown_until".format(stage=stage)] = float(
                stage_entry.get("cooldown_until", 0.0) or 0.0
            )
        return enriched

    def enrich_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        source_ids = [
            self._normalize_source_id(item.get("source_id", "")) for item in sources
        ]
        entries = self.get_many(source_ids)
        enriched_sources: list[dict[str, Any]] = []
        for source in sources:
            source_id = self._normalize_source_id(source.get("source_id", ""))
            entry = entries.get(source_id, _make_source_entry())
            enriched = dict(source)
            for stage in HEALTH_STAGES:
                stage_entry = entry[stage]
                enriched["{stage}_health_state".format(stage=stage)] = stage_entry.get(
                    "state", "unknown"
                )
                enriched["{stage}_health_summary".format(stage=stage)] = (
                    self._format_stage_summary(stage_entry)
                )
                enriched["{stage}_health_updated_at".format(stage=stage)] = float(
                    stage_entry.get("updated_at", 0.0) or 0.0
                )
                enriched["{stage}_health_consecutive_failures".format(stage=stage)] = (
                    int(stage_entry.get("consecutive_failures", 0) or 0)
                )
                enriched["{stage}_health_cooldown_until".format(stage=stage)] = float(
                    stage_entry.get("cooldown_until", 0.0) or 0.0
                )
            enriched_sources.append(enriched)
        return enriched_sources

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
            source_entry = self.get_source_health(normalized_source_id)
            stage_entry = dict(
                source_entry.get(normalized_stage) or _make_stage_entry()
            )
            stage_entry["state"] = state
            stage_entry["note"] = str(summary or "").strip()
            if state != "broken":
                stage_entry["last_error_code"] = ""
                stage_entry["last_error_summary"] = ""
                stage_entry["consecutive_failures"] = 0
                stage_entry["cooldown_until"] = 0.0
                stage_entry["last_failure_scope"] = ""
            stage_entry["updated_at"] = now
            self._merge_metadata(stage_entry, metadata)
            source_entry[normalized_stage] = stage_entry
            self._write_source_entry(
                source_id=normalized_source_id, source_entry=source_entry
            )
            return dict(stage_entry)

    def _initialize(self) -> None:
        with self._lock:
            with connect_sqlite(self.sqlite_path) as connection:
                self._ensure_schema(connection)

    def _migrate_json_if_needed(self) -> None:
        if not self.path.exists():
            return
        with self._lock:
            with connect_sqlite(self.sqlite_path) as connection:
                self._ensure_schema(connection)
                existing_count = connection.execute(
                    "SELECT COUNT(*) AS row_count FROM source_stage_health"
                ).fetchone()["row_count"]
                if int(existing_count or 0) > 0:
                    return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        sources = payload.get("sources", payload)
        if not isinstance(sources, dict):
            return
        rows: list[tuple[Any, ...]] = []
        for source_id, entry in sources.items():
            normalized_source_id = self._normalize_source_id(source_id)
            if not normalized_source_id or not isinstance(entry, dict):
                continue
            for stage in HEALTH_STAGES:
                stage_entry = entry.get(stage) or {}
                if not isinstance(stage_entry, dict):
                    stage_entry = {}
                merged = _make_stage_entry()
                metadata: dict[str, Any] = {}
                for key, value in stage_entry.items():
                    if key in merged:
                        merged[key] = value
                    else:
                        metadata[str(key)] = value
                rows.append(
                    (
                        normalized_source_id,
                        stage,
                        str(merged.get("state", "unknown") or "unknown"),
                        int(merged.get("attempts", 0) or 0),
                        int(merged.get("successes", 0) or 0),
                        int(merged.get("failures", 0) or 0),
                        int(merged.get("timeouts", 0) or 0),
                        int(merged.get("consecutive_failures", 0) or 0),
                        float(merged.get("cooldown_until", 0.0) or 0.0),
                        str(merged.get("last_failure_scope", "") or ""),
                        float(merged.get("avg_ms", 0.0) or 0.0),
                        float(merged.get("last_success_at", 0.0) or 0.0),
                        float(merged.get("last_failure_at", 0.0) or 0.0),
                        str(merged.get("last_error_code", "") or ""),
                        str(merged.get("last_error_summary", "") or ""),
                        str(merged.get("note", "") or ""),
                        float(merged.get("updated_at", 0.0) or 0.0),
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    )
                )
        if not rows:
            return
        with self._lock:
            with connect_sqlite(self.sqlite_path) as connection:
                connection.executemany(
                    """
                    INSERT INTO source_stage_health (
                        source_id,
                        stage,
                        state,
                        attempts,
                        successes,
                        failures,
                        timeouts,
                        consecutive_failures,
                        cooldown_until,
                        last_failure_scope,
                        avg_ms,
                        last_success_at,
                        last_failure_at,
                        last_error_code,
                        last_error_summary,
                        note,
                        updated_at,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, stage) DO NOTHING
                    """,
                    rows,
                )

    def _write_source_entry(
        self,
        source_id: str,
        source_entry: dict[str, dict[str, Any]],
    ) -> None:
        rows: list[tuple[Any, ...]] = []
        for stage in HEALTH_STAGES:
            stage_entry = dict(source_entry.get(stage) or _make_stage_entry())
            metadata = {
                key: value
                for key, value in stage_entry.items()
                if key not in _make_stage_entry()
            }
            rows.append(
                (
                    source_id,
                    stage,
                    str(stage_entry.get("state", "unknown") or "unknown"),
                    int(stage_entry.get("attempts", 0) or 0),
                    int(stage_entry.get("successes", 0) or 0),
                    int(stage_entry.get("failures", 0) or 0),
                    int(stage_entry.get("timeouts", 0) or 0),
                    int(stage_entry.get("consecutive_failures", 0) or 0),
                    float(stage_entry.get("cooldown_until", 0.0) or 0.0),
                    str(stage_entry.get("last_failure_scope", "") or ""),
                    float(stage_entry.get("avg_ms", 0.0) or 0.0),
                    float(stage_entry.get("last_success_at", 0.0) or 0.0),
                    float(stage_entry.get("last_failure_at", 0.0) or 0.0),
                    str(stage_entry.get("last_error_code", "") or ""),
                    str(stage_entry.get("last_error_summary", "") or ""),
                    str(stage_entry.get("note", "") or ""),
                    float(stage_entry.get("updated_at", 0.0) or 0.0),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                )
            )
        self._initialize()
        with connect_sqlite(self.sqlite_path) as connection:
            self._ensure_schema(connection)
            connection.executemany(
                """
                INSERT INTO source_stage_health (
                    source_id,
                    stage,
                    state,
                    attempts,
                    successes,
                    failures,
                    timeouts,
                    consecutive_failures,
                    cooldown_until,
                    last_failure_scope,
                    avg_ms,
                    last_success_at,
                    last_failure_at,
                    last_error_code,
                    last_error_summary,
                    note,
                    updated_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, stage) DO UPDATE SET
                    state=excluded.state,
                    attempts=excluded.attempts,
                    successes=excluded.successes,
                    failures=excluded.failures,
                    timeouts=excluded.timeouts,
                    consecutive_failures=excluded.consecutive_failures,
                    cooldown_until=excluded.cooldown_until,
                    last_failure_scope=excluded.last_failure_scope,
                    avg_ms=excluded.avg_ms,
                    last_success_at=excluded.last_success_at,
                    last_failure_at=excluded.last_failure_at,
                    last_error_code=excluded.last_error_code,
                    last_error_summary=excluded.last_error_summary,
                    note=excluded.note,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                rows,
            )

    def _ensure_schema(self, connection: Any) -> None:
        # Keep schema creation idempotent so teardown/reload races do not leave readers
        # talking to a fresh SQLite file without tables.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_stage_health (
                source_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'unknown',
                attempts INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                timeouts INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                cooldown_until REAL NOT NULL DEFAULT 0,
                last_failure_scope TEXT NOT NULL DEFAULT '',
                avg_ms REAL NOT NULL DEFAULT 0,
                last_success_at REAL NOT NULL DEFAULT 0,
                last_failure_at REAL NOT NULL DEFAULT 0,
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_summary TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (source_id, stage)
            )
            """
        )
        existing_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(source_stage_health)"
            ).fetchall()
        }
        required_columns = {
            "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
            "cooldown_until": "REAL NOT NULL DEFAULT 0",
            "last_failure_scope": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.execute(
                "ALTER TABLE source_stage_health ADD COLUMN {name} {definition}".format(
                    name=column_name,
                    definition=definition,
                )
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_stage_health_state
            ON source_stage_health (stage, state, updated_at DESC)
            """
        )
        connection.execute(
            "PRAGMA user_version = {version}".format(version=HEALTH_SCHEMA_VERSION)
        )

    def _row_to_stage_entry(self, row: Any) -> dict[str, Any]:
        stage_entry = _make_stage_entry()
        stage_entry.update(
            {
                "state": str(row["state"] or "unknown"),
                "attempts": int(row["attempts"] or 0),
                "successes": int(row["successes"] or 0),
                "failures": int(row["failures"] or 0),
                "timeouts": int(row["timeouts"] or 0),
                "consecutive_failures": int(row["consecutive_failures"] or 0),
                "cooldown_until": float(row["cooldown_until"] or 0.0),
                "last_failure_scope": str(row["last_failure_scope"] or ""),
                "avg_ms": float(row["avg_ms"] or 0.0),
                "last_success_at": float(row["last_success_at"] or 0.0),
                "last_failure_at": float(row["last_failure_at"] or 0.0),
                "last_error_code": str(row["last_error_code"] or ""),
                "last_error_summary": str(row["last_error_summary"] or ""),
                "note": str(row["note"] or ""),
                "updated_at": float(row["updated_at"] or 0.0),
            }
        )
        metadata_text = str(row["metadata_json"] or "").strip()
        if metadata_text:
            try:
                metadata = json.loads(metadata_text)
            except Exception:
                metadata = {}
            if isinstance(metadata, dict):
                for key, value in metadata.items():
                    if str(key or "").strip():
                        stage_entry[str(key)] = value
        return stage_entry

    def _merge_metadata(
        self, stage_entry: dict[str, Any], metadata: dict[str, Any] | None
    ) -> None:
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

    def _normalize_stage(self, stage: str) -> str:
        normalized = str(stage or "").strip().lower()
        if normalized not in HEALTH_STAGES:
            raise ValueError("未知健康阶段: {stage}".format(stage=stage))
        return normalized

    def _normalize_source_id(self, source_id: str) -> str:
        return str(source_id or "").strip()

    def _normalize_ids(self, source_ids: Iterable[str] | None) -> list[str] | None:
        if source_ids is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for source_id in source_ids:
            current = self._normalize_source_id(source_id)
            if not current or current in seen:
                continue
            seen.add(current)
            normalized.append(current)
        return normalized

    def _rolling_average(
        self, current: float, value: float, sample_count: int
    ) -> float:
        if sample_count <= 1:
            return round(value, 3)
        return round(((current * (sample_count - 1)) + value) / sample_count, 3)
