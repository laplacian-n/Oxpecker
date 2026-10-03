"""Working Notebook store — SQLite WAL, one DB per engagement (notebook.db). Pure persistence +
structural invariants (ordinal assignment, append-only event log kept in sync with the
materialized `notes` table). Judgment-free: the service decides what a note is, the store just
records it. Mirrors agent/hypothesis_graph/store.py's conventions.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .schema import EVENT_KINDS, SCHEMA, SCHEMA_VERSION, Category, NoteStatus


class NotFoundError(RuntimeError):
    pass


class NotebookValidationError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"n_{uuid.uuid4().hex[:12]}"


class NotebookStore:
    def __init__(self, engagement_dir: Path):
        engagement_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = engagement_dir / "notebook.db"
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("INSERT OR IGNORE INTO notebook_state (id, version, next_ordinal) VALUES (1, 0, 1)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _emit(self, conn, kind: str, payload: dict, *, note_id: str | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise NotebookValidationError(f"unknown event kind {kind!r}")
        conn.execute(
            "INSERT INTO events (kind, note_id, payload_json, created_at) VALUES (?,?,?,?)",
            (kind, note_id, json.dumps(payload), _now()),
        )
        conn.execute("UPDATE notebook_state SET version = version + 1 WHERE id = 1")

    def version(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT version FROM notebook_state WHERE id=1").fetchone()[0]

    # -- mutations -------------------------------------------------------------------------------

    def add_note(
        self, *, category: str, note: str, tags: list[str] | None = None,
        surface: str | None = None, refs: list[str] | None = None,
        chat_message_id: str | None = None,
    ) -> dict:
        Category(category)  # raises ValueError if not a known category
        text = (note or "").strip()
        if not text:
            raise NotebookValidationError("a note needs text")
        tags = [t.strip().lower() for t in (tags or []) if t.strip()]
        refs = [r.strip() for r in (refs or []) if r.strip()]
        with self._connect() as conn:
            ordinal = conn.execute("SELECT next_ordinal FROM notebook_state WHERE id=1").fetchone()[0]
            conn.execute("UPDATE notebook_state SET next_ordinal = next_ordinal + 1 WHERE id=1")
            nid = _new_id()
            now = _now()
            conn.execute(
                "INSERT INTO notes (note_id, ordinal, category, note, tags_json, surface, refs_json, "
                "status, chat_message_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (nid, ordinal, category, text, json.dumps(tags), surface, json.dumps(refs),
                 NoteStatus.OPEN.value, chat_message_id, now, now),
            )
            self._emit(conn, "note.added",
                       {"ordinal": ordinal, "category": category, "note": text}, note_id=nid)
            return {"note_id": nid, "ordinal": ordinal}

    def add_ref(self, note_id: str, ref: str) -> None:
        with self._connect() as conn:
            r = conn.execute("SELECT refs_json FROM notes WHERE note_id=?", (note_id,)).fetchone()
            if r is None:
                raise NotFoundError(note_id)
            refs = json.loads(r["refs_json"])
            if ref in refs:
                return
            refs.append(ref)
            conn.execute("UPDATE notes SET refs_json=?, updated_at=? WHERE note_id=?",
                         (json.dumps(refs), _now(), note_id))
            self._emit(conn, "note.edited", {"added_ref": ref}, note_id=note_id)

    def set_status(self, note_id: str, status: str, reason: str | None = None) -> None:
        NoteStatus(status)
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM notes WHERE note_id=?", (note_id,)).fetchone()
            if row is None:
                raise NotFoundError(note_id)
            conn.execute(
                "UPDATE notes SET status=?, resolved_reason=?, updated_at=? WHERE note_id=?",
                (status, reason if status == NoteStatus.RESOLVED.value else None, _now(), note_id),
            )
            self._emit(
                conn, "note.resolved" if status == NoteStatus.RESOLVED.value else "note.reopened",
                {"status": status, "reason": reason}, note_id=note_id,
            )

    # -- reads ----------------------------------------------------------------------------------

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["tags"] = json.loads(d.pop("tags_json"))
        d["refs"] = json.loads(d.pop("refs_json"))
        return d

    def get(self, note_id: str) -> dict:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM notes WHERE note_id=?", (note_id,)).fetchone()
            if r is None:
                raise NotFoundError(note_id)
            return self._row(r)

    def get_by_ordinal(self, ordinal: int) -> dict:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM notes WHERE ordinal=?", (ordinal,)).fetchone()
            if r is None:
                raise NotFoundError(f"N-{ordinal}")
            return self._row(r)

    def list_notes(self, *, category: str | None = None, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM notes"
        clauses, params = [], []
        if category:
            clauses.append("category=?")
            params.append(category)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY ordinal DESC"
        with self._connect() as conn:
            return [self._row(r) for r in conn.execute(q, params).fetchall()]

    def list_events(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY event_seq").fetchall()]

    def check_invariants(self) -> list[str]:
        problems: list[str] = []
        with self._connect() as conn:
            ords = [r["ordinal"] for r in conn.execute("SELECT ordinal FROM notes").fetchall()]
            if len(ords) != len(set(ords)):
                problems.append("duplicate ordinals present")
            added = {r["note_id"] for r in conn.execute(
                "SELECT note_id FROM events WHERE kind='note.added'").fetchall()}
            for r in conn.execute("SELECT note_id FROM notes").fetchall():
                if r["note_id"] not in added:
                    problems.append(f"note {r['note_id']} has no added event")
        return problems
