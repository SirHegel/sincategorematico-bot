from __future__ import annotations

from pathlib import Path
import sqlite3
import time


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        kind TEXT NOT NULL,
        message TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        added_at INTEGER NOT NULL,
        last_fetch_at INTEGER,
        last_status TEXT,
        etag TEXT,
        modified TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
        guid TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        published_at INTEGER,
        discovered_at INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'new',
        attempts INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
        body TEXT NOT NULL,
        link TEXT,
        title TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT 'auto',
        state TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        decided_at INTEGER,
        published_at INTEGER,
        notified_message_id INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        post_urn TEXT,
        publish_started_at INTEGER,
        retry_at INTEGER,
        simulated_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_items_state ON items(state, discovered_at)",
    "CREATE INDEX IF NOT EXISTS idx_drafts_state ON drafts(state, created_at)",
)

DRAFT_STATES = (
    "pending", "approved", "publishing", "uncertain", "rejected",
    "published", "failed", "discarded",
)

# Columnas añadidas después de la primera versión: se aplican sobre bases ya creadas.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("items", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("drafts", "publish_started_at", "INTEGER"),
    ("drafts", "retry_at", "INTEGER"),
    ("drafts", "simulated_at", "INTEGER"),
)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        for statement in SCHEMA:
            self._connection.execute(statement)
        self._migrate()
        self._connection.commit()
        self.path.chmod(0o600)

    def _migrate(self) -> None:
        for table, column, definition in MIGRATIONS:
            existing = {
                str(row["name"])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if existing and column not in existing:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # -- ajustes ----------------------------------------------------------

    def get(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def get_int(self, key: str) -> int | None:
        value = self.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

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

    def set_default(self, key: str, value: str | int | bool) -> None:
        if self.get(key) is None:
            self.set(key, value)

    def delete(self, key: str) -> None:
        self._connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        self._connection.commit()

    # -- actividad --------------------------------------------------------

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
        return [
            {"created_at": int(row["created_at"]), "kind": str(row["kind"]), "message": str(row["message"])}
            for row in rows
        ]

    # -- fuentes ----------------------------------------------------------

    def add_source(self, url: str, name: str) -> int | None:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO sources(url, name, added_at) VALUES(?, ?, ?)",
            (url, name[:80], int(time.time())),
        )
        self._connection.commit()
        return cursor.lastrowid if cursor.rowcount else None

    def remove_source(self, source_id: int) -> bool:
        cursor = self._connection.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self._connection.commit()
        return cursor.rowcount > 0

    def set_source_enabled(self, source_id: int, enabled: bool) -> bool:
        cursor = self._connection.execute(
            "UPDATE sources SET enabled = ? WHERE id = ?", (1 if enabled else 0, source_id)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def update_source(self, source_id: int, *, url: str, name: str) -> bool:
        cursor = self._connection.execute(
            "UPDATE sources SET url = ?, name = ? WHERE id = ?",
            (url, name[:80], source_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def sources(self, *, only_enabled: bool = False) -> list[dict[str, object]]:
        query = "SELECT * FROM sources"
        if only_enabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        return [dict(row) for row in self._connection.execute(query).fetchall()]

    def mark_source_fetched(
        self, source_id: int, status: str, *, etag: str | None = None, modified: str | None = None
    ) -> None:
        self._connection.execute(
            "UPDATE sources SET last_fetch_at = ?, last_status = ?, etag = ?, modified = ? WHERE id = ?",
            (int(time.time()), status[:80], etag, modified, source_id),
        )
        self._connection.commit()

    # -- noticias ---------------------------------------------------------

    def add_item(
        self,
        *,
        source_id: int | None,
        guid: str,
        url: str,
        title: str,
        summary: str,
        published_at: int | None,
    ) -> int | None:
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO items(source_id, guid, url, title, summary, published_at, discovered_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, guid, url, title[:300], summary[:2000], published_at, int(time.time())),
        )
        self._connection.commit()
        return cursor.lastrowid if cursor.rowcount else None

    def next_item(self, *, max_age_seconds: int) -> dict[str, object] | None:
        threshold = int(time.time()) - max_age_seconds
        row = self._connection.execute(
            """
            SELECT items.*, sources.name AS source_name
            FROM items LEFT JOIN sources ON sources.id = items.source_id
            WHERE items.state = 'new'
              AND COALESCE(items.published_at, items.discovered_at) >= ?
            ORDER BY items.attempts ASC, COALESCE(items.published_at, items.discovered_at) DESC
            LIMIT 1
            """,
            (threshold,),
        ).fetchone()
        return None if row is None else dict(row)

    def set_item_state(self, item_id: int, state: str) -> None:
        self._connection.execute("UPDATE items SET state = ? WHERE id = ?", (state, item_id))
        self._connection.commit()

    def bump_item_attempts(self, item_id: int) -> int:
        self._connection.execute(
            "UPDATE items SET attempts = attempts + 1 WHERE id = ?", (item_id,)
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT attempts FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return 0 if row is None else int(row["attempts"])

    def count_items_by_state(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT state, COUNT(*) AS total FROM items GROUP BY state"
        ).fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def expire_items(self, *, max_age_seconds: int) -> int:
        threshold = int(time.time()) - max_age_seconds
        cursor = self._connection.execute(
            "UPDATE items SET state = 'skipped' WHERE state = 'new' "
            "AND COALESCE(published_at, discovered_at) < ?",
            (threshold,),
        )
        self._connection.commit()
        return cursor.rowcount

    def prune_items(self, *, keep: int = 1000) -> None:
        self._connection.execute(
            "DELETE FROM items WHERE state != 'new' AND id NOT IN "
            "(SELECT id FROM items ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        self._connection.commit()

    # -- borradores -------------------------------------------------------

    def add_draft(
        self, *, item_id: int | None, body: str, link: str | None, title: str, origin: str = "auto"
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO drafts(item_id, body, link, title, origin, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (item_id, body, link, title[:300], origin, int(time.time())),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def draft(self, draft_id: int) -> dict[str, object] | None:
        row = self._connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return None if row is None else dict(row)

    def drafts_by_state(self, state: str, *, limit: int = 20) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM drafts WHERE state = ? ORDER BY id LIMIT ?",
            (state, max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def next_approved_for_simulation(self) -> dict[str, object] | None:
        """Devuelve el aprobado más antiguo que aún no pasó por dry-run."""

        row = self._connection.execute(
            "SELECT * FROM drafts WHERE state = 'approved' "
            "AND simulated_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        return None if row is None else dict(row)

    def claim_next_approved(self, *, now: int | None = None) -> dict[str, object] | None:
        """Reserva un borrador antes de hacer el POST externo.

        La transición se confirma antes de tocar LinkedIn. Si el proceso cae
        después, el arranque lo mueve a ``uncertain`` y nunca lo duplica solo.
        """
        stamp = int(time.time()) if now is None else int(now)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM drafts WHERE state = 'approved' "
                "AND COALESCE(retry_at, 0) <= ? ORDER BY id LIMIT 1",
                (stamp,),
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            cursor = self._connection.execute(
                "UPDATE drafts SET state = 'publishing', publish_started_at = ?, "
                "last_error = NULL WHERE id = ? AND state = 'approved'",
                (stamp, int(row["id"])),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                return None
            self._connection.commit()
            claimed = dict(row)
            claimed["state"] = "publishing"
            claimed["publish_started_at"] = stamp
            return claimed
        except Exception:
            self._connection.rollback()
            raise

    def recover_inflight_publications(self) -> int:
        """Congela envíos cuyo resultado externo se perdió por una caída."""
        cursor = self._connection.execute(
            "UPDATE drafts SET state = 'uncertain', "
            "last_error = COALESCE(last_error, "
            "'El proceso se interrumpió durante el envío; verifica LinkedIn antes de reintentar') "
            "WHERE state = 'publishing'"
        )
        self._connection.commit()
        return cursor.rowcount

    def schedule_draft_retry(self, draft_id: int, *, retry_at: int, error: str) -> int:
        self._connection.execute(
            "UPDATE drafts SET state = 'approved', attempts = attempts + 1, "
            "retry_at = ?, publish_started_at = NULL, last_error = ? WHERE id = ?",
            (int(retry_at), error[:240], draft_id),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT attempts FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return 0 if row is None else int(row["attempts"])

    def mark_draft_uncertain(self, draft_id: int, error: str) -> None:
        self._connection.execute(
            "UPDATE drafts SET state = 'uncertain', attempts = attempts + 1, "
            "last_error = ?, retry_at = NULL WHERE id = ?",
            (error[:240], draft_id),
        )
        self._connection.commit()

    def retry_draft(self, draft_id: int) -> bool:
        cursor = self._connection.execute(
            "UPDATE drafts SET state = 'approved', attempts = 0, last_error = NULL, "
            "retry_at = NULL, publish_started_at = NULL "
            "WHERE id = ? AND state IN ('failed', 'uncertain')",
            (draft_id,),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def mark_draft_simulated(self, draft_id: int) -> None:
        self._connection.execute(
            "UPDATE drafts SET simulated_at = ? WHERE id = ? AND state = 'approved'",
            (int(time.time()), draft_id),
        )
        self._connection.commit()

    def recent_drafts(self, limit: int = 20) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)
        ).fetchall()
        return [dict(row) for row in rows]

    def unnotified_drafts(self, *, limit: int = 5) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM drafts WHERE state = 'pending' AND notified_message_id IS NULL "
            "ORDER BY id LIMIT ?",
            (max(1, min(limit, 20)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_draft_notified(self, draft_id: int, message_id: int) -> None:
        self._connection.execute(
            "UPDATE drafts SET notified_message_id = ? WHERE id = ?", (message_id, draft_id)
        )
        self._connection.commit()

    def set_draft_state(self, draft_id: int, state: str, *, error: str | None = None) -> bool:
        if state not in DRAFT_STATES:
            raise ValueError(f"Estado de borrador inválido: {state}")
        decided = int(time.time()) if state in {"approved", "rejected", "discarded"} else None
        cursor = self._connection.execute(
            """
            UPDATE drafts
            SET state = ?,
                decided_at = COALESCE(?, decided_at),
                last_error = ?,
                retry_at = NULL,
                publish_started_at = CASE WHEN ? = 'publishing' THEN publish_started_at ELSE NULL END
            WHERE id = ?
            """,
            (state, decided, error[:240] if error else None, state, draft_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def mark_draft_published(self, draft_id: int, post_urn: str | None) -> None:
        self._connection.execute(
            "UPDATE drafts SET state = 'published', published_at = ?, post_urn = ?, "
            "last_error = NULL, retry_at = NULL, publish_started_at = NULL "
            "WHERE id = ?",
            (int(time.time()), post_urn, draft_id),
        )
        self._connection.commit()

    def reconcile_draft_as_published(self, draft_id: int, post_urn: str) -> bool:
        """Confirma manualmente un envío incierto sin repetir el POST externo."""

        urn = post_urn.strip()
        if not urn.startswith("urn:li:"):
            raise ValueError("URN de publicación inválida")
        cursor = self._connection.execute(
            "UPDATE drafts SET state = 'published', published_at = ?, post_urn = ?, "
            "last_error = NULL, retry_at = NULL, publish_started_at = NULL "
            "WHERE id = ? AND state = 'uncertain'",
            (int(time.time()), urn, draft_id),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def bump_draft_attempts(self, draft_id: int) -> int:
        self._connection.execute(
            "UPDATE drafts SET attempts = attempts + 1 WHERE id = ?", (draft_id,)
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT attempts FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return 0 if row is None else int(row["attempts"])

    def count_published_since(self, since: int) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM drafts WHERE state = 'published' AND published_at >= ?",
            (since,),
        ).fetchone()
        return int(row[0])

    def last_published_at(self) -> int | None:
        row = self._connection.execute(
            "SELECT published_at FROM drafts WHERE state = 'published' "
            "ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def count_by_state(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT state, COUNT(*) AS total FROM drafts GROUP BY state"
        ).fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def close(self) -> None:
        self._connection.close()
