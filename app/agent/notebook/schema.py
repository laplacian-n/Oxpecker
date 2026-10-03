"""Working Notebook — MVP 0 semantics and storage schema.

A per-engagement lab notebook (notebook.db beside state.db / hypothesis_graph.db). See
docs/working-notebook-spec.md. Same storage conventions as agent/hypothesis_graph/store.py:
SQLite WAL, an append-only event log the materialized `notes` table is rebuildable from, stable
ULID-style ids with display ordinals that are never reused.

The notebook is deliberately NOT the hypothesis graph, the findings store, or the observation
records — a note is raw working material (a cross-cutting insight, a reusable technique, a todo,
a ruled-out dead-end), and it references those other stores by id rather than duplicating them.
"""
from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    RECON = "recon"
    AUTH = "auth"
    ACCESS_CONTROL = "access-control"
    INJECTION = "injection"
    CRYPTO = "crypto"
    CONFIG = "config"
    BUSINESS_LOGIC = "business-logic"
    INFRA = "infra"
    TECHNIQUE = "technique"        # reusable know-how
    TODO = "todo"                  # actionable, has an open/resolved lifecycle
    DEAD_END = "dead-end"          # ruled out — do not retry
    MISC = "misc"


# Categories whose open/resolved lifecycle actually means something (a resolve on any other
# category is accepted but advisory — you can still close a stale insight).
LIFECYCLE_CATEGORIES = frozenset({Category.TODO.value, Category.DEAD_END.value})


class NoteStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


EVENT_KINDS = ("note.added", "note.resolved", "note.reopened", "note.edited")

SCHEMA_VERSION = 1

# One executescript() at store init. `events` is the append-only source of truth; `notes` is the
# fast materialized read model kept in sync with it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    event_seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    note_id      TEXT,
    payload_json TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    note_id         TEXT PRIMARY KEY,
    ordinal         INTEGER NOT NULL,
    category        TEXT NOT NULL,
    note            TEXT NOT NULL,
    tags_json       TEXT NOT NULL DEFAULT '[]',
    surface         TEXT,
    refs_json       TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'open',
    resolved_reason TEXT,
    chat_message_id TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notebook_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    version      INTEGER NOT NULL DEFAULT 0,
    next_ordinal INTEGER NOT NULL DEFAULT 1
);
"""
