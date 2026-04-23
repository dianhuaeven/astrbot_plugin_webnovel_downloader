from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from .sqlite_support import connect_sqlite, derive_sqlite_path


SEARCH_STATS_SCHEMA_VERSION = 1


def _make_search_stats_entry() -> dict[str, Any]:
    return {
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "timeouts": 0,
        "avg_duration_ms": 0.0,
        "avg_success_ms": 0.0,
        "last_success_at": 0.0,
        "last_failure_at": 0.0,
        "updated_at": 0.0,
    }


class SearchStatsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sqlite_path = derive_sqlite_path(self.path)
        self._lock = threading.RLock()
        self._initialize()
        self._migrate_json_if_needed()

    def load_all(self) -> dict[str, dict[str, Any]]:
        return self.get_many()

    def get_many(
        self, source_ids: Iterable[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = self._normalize_ids(source_ids)
        with self._lock:
            self._initialize()
            with connect_sqlite(self.sqlite_path) as connection:
                if normalized_ids is None:
                    rows = connection.execute(
                        """
                        SELECT
                            source_id,
                            attempts,
                            successes,
                            failures,
                            timeouts,
                            avg_duration_ms,
                            avg_success_ms,
                            last_success_at,
                            last_failure_at,
                            updated_at
                        FROM search_source_stats
                        """
                    ).fetchall()
                elif not normalized_ids:
                    rows = []
                else:
                    placeholders = ",".join("?" for _ in normalized_ids)
                    rows = connection.execute(
                        """
                        SELECT
                            source_id,
                            attempts,
                            successes,
                            failures,
                            timeouts,
                            avg_duration_ms,
                            avg_success_ms,
                            last_success_at,
                            last_failure_at,
                            updated_at
                        FROM search_source_stats
                        WHERE source_id IN ({placeholders})
                        """.format(placeholders=placeholders),
                        normalized_ids,
                    ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result[str(row["source_id"])] = self._row_to_entry(row)
        return result

    def apply_outcomes(
        self, outcomes: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        normalized_outcomes = [
            dict(item)
            for item in list(outcomes or [])
            if str(item.get("source_id") or "").strip()
        ]
        if not normalized_outcomes:
            return {}
        source_ids = sorted(
            {str(item.get("source_id") or "").strip() for item in normalized_outcomes}
        )
        with self._lock:
            self._initialize()
            existing = self.get_many(source_ids)
            updated: dict[str, dict[str, Any]] = {
                source_id: dict(existing.get(source_id) or _make_search_stats_entry())
                for source_id in source_ids
            }
            for outcome in normalized_outcomes:
                self._apply_outcome(updated[str(outcome["source_id"]).strip()], outcome)
            with connect_sqlite(self.sqlite_path) as connection:
                connection.executemany(
                    """
                    INSERT INTO search_source_stats (
                        source_id,
                        attempts,
                        successes,
                        failures,
                        timeouts,
                        avg_duration_ms,
                        avg_success_ms,
                        last_success_at,
                        last_failure_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        attempts=excluded.attempts,
                        successes=excluded.successes,
                        failures=excluded.failures,
                        timeouts=excluded.timeouts,
                        avg_duration_ms=excluded.avg_duration_ms,
                        avg_success_ms=excluded.avg_success_ms,
                        last_success_at=excluded.last_success_at,
                        last_failure_at=excluded.last_failure_at,
                        updated_at=excluded.updated_at
                    """,
                    [
                        (
                            source_id,
                            int(entry.get("attempts", 0) or 0),
                            int(entry.get("successes", 0) or 0),
                            int(entry.get("failures", 0) or 0),
                            int(entry.get("timeouts", 0) or 0),
                            float(entry.get("avg_duration_ms", 0.0) or 0.0),
                            float(entry.get("avg_success_ms", 0.0) or 0.0),
                            float(entry.get("last_success_at", 0.0) or 0.0),
                            float(entry.get("last_failure_at", 0.0) or 0.0),
                            float(entry.get("updated_at", 0.0) or 0.0),
                        )
                        for source_id, entry in updated.items()
                    ],
                )
        return updated

    def _initialize(self) -> None:
        with self._lock:
            with connect_sqlite(self.sqlite_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_source_stats (
                        source_id TEXT PRIMARY KEY,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        successes INTEGER NOT NULL DEFAULT 0,
                        failures INTEGER NOT NULL DEFAULT 0,
                        timeouts INTEGER NOT NULL DEFAULT 0,
                        avg_duration_ms REAL NOT NULL DEFAULT 0,
                        avg_success_ms REAL NOT NULL DEFAULT 0,
                        last_success_at REAL NOT NULL DEFAULT 0,
                        last_failure_at REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    "PRAGMA user_version = {version}".format(
                        version=SEARCH_STATS_SCHEMA_VERSION
                    )
                )

    def _migrate_json_if_needed(self) -> None:
        if not self.path.exists():
            return
        with self._lock:
            with connect_sqlite(self.sqlite_path) as connection:
                existing_count = connection.execute(
                    "SELECT COUNT(*) AS row_count FROM search_source_stats"
                ).fetchone()["row_count"]
                if int(existing_count or 0) > 0:
                    return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return
        sources = payload.get("sources", payload) if isinstance(payload, dict) else {}
        if not isinstance(sources, dict):
            return
        rows = []
        for source_id, entry in sources.items():
            normalized_source_id = str(source_id or "").strip()
            if not normalized_source_id or not isinstance(entry, dict):
                continue
            merged = _make_search_stats_entry()
            for key in list(merged):
                if key in entry:
                    merged[key] = entry[key]
            rows.append(
                (
                    normalized_source_id,
                    int(merged.get("attempts", 0) or 0),
                    int(merged.get("successes", 0) or 0),
                    int(merged.get("failures", 0) or 0),
                    int(merged.get("timeouts", 0) or 0),
                    float(merged.get("avg_duration_ms", 0.0) or 0.0),
                    float(merged.get("avg_success_ms", 0.0) or 0.0),
                    float(merged.get("last_success_at", 0.0) or 0.0),
                    float(merged.get("last_failure_at", 0.0) or 0.0),
                    float(merged.get("updated_at", 0.0) or 0.0),
                )
            )
        if not rows:
            return
        with self._lock:
            with connect_sqlite(self.sqlite_path) as connection:
                connection.executemany(
                    """
                    INSERT INTO search_source_stats (
                        source_id,
                        attempts,
                        successes,
                        failures,
                        timeouts,
                        avg_duration_ms,
                        avg_success_ms,
                        last_success_at,
                        last_failure_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO NOTHING
                    """,
                    rows,
                )

    def _apply_outcome(self, entry: dict[str, Any], outcome: dict[str, Any]) -> None:
        elapsed_ms = max(0.0, float(outcome.get("elapsed_ms", 0.0) or 0.0))
        entry["attempts"] = int(entry.get("attempts", 0) or 0) + 1
        entry["avg_duration_ms"] = self._rolling_average(
            float(entry.get("avg_duration_ms", 0.0) or 0.0),
            elapsed_ms,
            int(entry["attempts"]),
        )
        recorded_at = float(outcome.get("recorded_at", 0.0) or 0.0)
        if outcome.get("success"):
            entry["successes"] = int(entry.get("successes", 0) or 0) + 1
            entry["avg_success_ms"] = self._rolling_average(
                float(entry.get("avg_success_ms", 0.0) or 0.0),
                elapsed_ms,
                int(entry["successes"]),
            )
            entry["last_success_at"] = recorded_at
        else:
            entry["failures"] = int(entry.get("failures", 0) or 0) + 1
            if outcome.get("timed_out"):
                entry["timeouts"] = int(entry.get("timeouts", 0) or 0) + 1
            entry["last_failure_at"] = recorded_at
        entry["updated_at"] = recorded_at

    def _row_to_entry(self, row: Any) -> dict[str, Any]:
        return {
            "attempts": int(row["attempts"] or 0),
            "successes": int(row["successes"] or 0),
            "failures": int(row["failures"] or 0),
            "timeouts": int(row["timeouts"] or 0),
            "avg_duration_ms": float(row["avg_duration_ms"] or 0.0),
            "avg_success_ms": float(row["avg_success_ms"] or 0.0),
            "last_success_at": float(row["last_success_at"] or 0.0),
            "last_failure_at": float(row["last_failure_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def _normalize_ids(self, source_ids: Iterable[str] | None) -> list[str] | None:
        if source_ids is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for source_id in source_ids:
            current = str(source_id or "").strip()
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
