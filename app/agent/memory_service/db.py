"""SQLite (WAL mode) storage for the memory service — §11 session/memory data model.

WAL mode handles multi-client reads + serialized writes at personal/small-team scale (doc's
own sizing call). The `sessions.version` column is the optimistic-concurrency counter: every
append bumps it inside the same transaction as the insert, so a client appending against a
stale version gets a conflict instead of silently interleaving with another active loop.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    version      INTEGER NOT NULL DEFAULT 0,
    deleted_at   REAL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id       TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    role             TEXT NOT NULL,
    origin_device    TEXT NOT NULL,
    content          TEXT NOT NULL,
    metadata_json    TEXT,
    created_at       REAL NOT NULL,
    schema_version   INTEGER NOT NULL,
    sensitivity_label TEXT NOT NULL DEFAULT 'normal',
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS condensed (
    session_id          TEXT PRIMARY KEY,
    version             INTEGER NOT NULL,
    content             TEXT NOT NULL,
    source_seq_start    INTEGER NOT NULL,
    source_seq_end      INTEGER NOT NULL,
    summarizer_config   TEXT NOT NULL,
    created_at          REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
"""


class ConflictError(RuntimeError):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"version conflict: current version is {current_version}")


class NotFoundError(RuntimeError):
    pass


class Store:
    def __init__(self, db_path: Path = config.MEMORY_SERVICE_DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- sessions ---------------------------------------------------------------------------

    def ensure_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, created_at, version) VALUES (?,?,0)",
                (session_id, time.time()),
            )

    def get_session(self, session_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=? AND deleted_at IS NULL", (session_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(session_id)
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()["n"]
            return {
                "session_id": row["session_id"],
                "created_at": row["created_at"],
                "version": row["version"],
                "message_count": count,
            }

    def list_sessions(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, created_at, version FROM sessions WHERE deleted_at IS NULL "
                "ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        """Full deletion — messages and condensed memory removed, not just tombstoned, per
        the Phase-2 exit criterion ("full deletion demonstrated from a clean environment")."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM condensed WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            return True

    # -- messages (optimistic concurrency on sessions.version) ------------------------------

    def add_items(
        self, session_id: str, items: list[dict], origin_device: str, expected_version: int
    ) -> int:
        """items: [{"role":..., "content":..., "sensitivity_label": optional}]. Returns the
        new session version. Raises ConflictError if expected_version doesn't match current."""
        self.ensure_session(session_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            current_version = row["version"]
            if current_version != expected_version:
                raise ConflictError(current_version)

            next_seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
            seq = next_seq_row["next_seq"]

            now = time.time()
            for item in items:
                metadata = item.get("metadata")
                conn.execute(
                    "INSERT INTO messages (message_id, session_id, seq, role, origin_device, "
                    "content, metadata_json, created_at, schema_version, sensitivity_label) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        session_id,
                        seq,
                        item["role"],
                        origin_device,
                        item["content"],
                        json.dumps(metadata) if metadata else None,
                        now,
                        SCHEMA_VERSION,
                        item.get("sensitivity_label", "normal"),
                    ),
                )
                seq += 1

            new_version = current_version + 1
            conn.execute(
                "UPDATE sessions SET version=? WHERE session_id=?", (new_version, session_id)
            )
            return new_version

    def get_items(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY seq ASC", (session_id,)
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d.pop("metadata_json")) if d["metadata_json"] else None
                out.append(d)
            return out

    # -- condensed memory ---------------------------------------------------------------------

    def set_condensed(
        self,
        session_id: str,
        content: str,
        source_seq_start: int,
        source_seq_end: int,
        summarizer_config: str,
    ) -> int:
        self.ensure_session(session_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM condensed WHERE session_id=?", (session_id,)
            ).fetchone()
            new_version = (row["version"] + 1) if row else 1
            conn.execute(
                "INSERT INTO condensed (session_id, version, content, source_seq_start, "
                "source_seq_end, summarizer_config, created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET version=excluded.version, "
                "content=excluded.content, source_seq_start=excluded.source_seq_start, "
                "source_seq_end=excluded.source_seq_end, "
                "summarizer_config=excluded.summarizer_config, created_at=excluded.created_at",
                (
                    session_id,
                    new_version,
                    content,
                    source_seq_start,
                    source_seq_end,
                    summarizer_config,
                    time.time(),
                ),
            )
            return new_version

    def get_condensed(self, session_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM condensed WHERE session_id=?", (session_id,)
            ).fetchone()
            return dict(row) if row else None
