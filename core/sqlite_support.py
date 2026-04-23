from __future__ import annotations

import sqlite3
from pathlib import Path


def derive_sqlite_path(
    path: str | Path, default_name: str = "plugin_state.sqlite3"
) -> Path:
    raw_path = Path(path)
    if raw_path.suffix:
        return raw_path.with_suffix(".sqlite3")
    if raw_path.name:
        return raw_path.with_name(raw_path.name + ".sqlite3")
    return raw_path / default_name


def connect_sqlite(path: str | Path) -> sqlite3.Connection:
    sqlite_path = Path(path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(sqlite_path),
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection
