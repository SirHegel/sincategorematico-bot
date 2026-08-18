from __future__ import annotations

from pathlib import Path
import sqlite3
import time


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        self._connection.commit()
        path.chmod(0o600)

    def get(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def get_int(self, key: str) -> int | None:
        value = self.get(key)
        return None if value is None else int(value)

    def get_bool(self, key: str, *, default: bool = False) -> bool:
        value = self.get(key)
        if value is None:
            return default
        return value == "1"

    def set(self, key: str, value: str | int | bool) -> None:
        if isinstance(value, bool):
            serialized = "1" if value else "0"
        else:
            serialized = str(value)
        self._connection.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, serialized),
        )
        self._connection.commit()

    def add_activity(self, kind: str, message: str) -> None:
        self._connection.execute(
            "INSERT INTO activity(created_at, kind, message) VALUES(?, ?, ?)",
            (int(time.time()), kind[:32], message[:240]),
        )
        self._connection.execute(
            "DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 200)"
        )
        self._connection.commit()

    def recent_activity(self, limit: int = 20) -> list[dict[str, int | str]]:
        rows = self._connection.execute(
            "SELECT created_at, kind, message FROM activity ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [{"created_at": int(row[0]), "kind": str(row[1]), "message": str(row[2])} for row in rows]

    def close(self) -> None:
        self._connection.close()
