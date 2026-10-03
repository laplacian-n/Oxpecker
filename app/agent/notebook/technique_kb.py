"""Cross-engagement technique KB — the ONE global store `technique` notebook notes overflow
into, so a reusable trick learned on one engagement is recallable on the next
(docs/working-notebook-spec.md §7). SQLite, at config.TECHNIQUE_KB_PATH, not engagement-scoped.

Dedup on save (normalized title+body). Two recall paths: `recall()` — exact keyword/tag/surface
substring matching, kept as-is for backward compatibility — and `semantic_recall()`, a hybrid of
that same keyword bonus plus TF-IDF cosine similarity (`kb_vectors.py`), so a query phrased
differently from the saved technique's wording can still surface it. `semantic_recall()` is what
closes the "auto-relevance scoring against the current target" gap docs/working-notebook-spec.md
§7 used to list as deferred — `NotebookService.build_context_block()` now calls it every turn
with the session's own recent notes as the query, so relevant past-engagement techniques get
auto-surfaced without the agent having to call technique_recall first.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import config
from .kb_vectors import TfidfIndex

_SCHEMA = """
CREATE TABLE IF NOT EXISTS techniques (
    technique_id      TEXT PRIMARY KEY,
    ordinal           INTEGER NOT NULL,
    norm_key          TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    body              TEXT NOT NULL,
    tags_json         TEXT NOT NULL DEFAULT '[]',
    surfaces_json     TEXT NOT NULL DEFAULT '[]',
    source_engagement TEXT,
    created_at        REAL NOT NULL,
    use_count         INTEGER NOT NULL DEFAULT 0,
    last_used_at      REAL
);
CREATE TABLE IF NOT EXISTS kb_state (id INTEGER PRIMARY KEY CHECK (id=1), next_ordinal INTEGER NOT NULL DEFAULT 1);
"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


class TechniqueKB:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else config.TECHNIQUE_KB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO kb_state (id, next_ordinal) VALUES (1, 1)")

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

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["tags"] = json.loads(d.pop("tags_json"))
        d["surfaces"] = json.loads(d.pop("surfaces_json"))
        d.pop("norm_key", None)
        return d

    def save(self, *, title: str, body: str, tags: list[str] | None = None,
             surfaces: list[str] | None = None, source_engagement: str | None = None) -> dict:
        """Idempotent: a technique whose normalized title|body already exists is not re-added
        (its tags/surfaces are merged instead). Returns {ordinal, deduped}."""
        title = (title or "").strip()
        body = (body or "").strip()
        norm = _norm(f"{title}|{body}")
        tags = sorted({t.strip().lower() for t in (tags or []) if t.strip()})
        surfaces = sorted({s.strip() for s in (surfaces or []) if s.strip()})
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM techniques WHERE norm_key=?", (norm,)).fetchone()
            if existing:
                merged_tags = sorted(set(json.loads(existing["tags_json"])) | set(tags))
                merged_surf = sorted(set(json.loads(existing["surfaces_json"])) | set(surfaces))
                conn.execute("UPDATE techniques SET tags_json=?, surfaces_json=? WHERE technique_id=?",
                             (json.dumps(merged_tags), json.dumps(merged_surf), existing["technique_id"]))
                return {"ordinal": existing["ordinal"], "deduped": True}
            ordinal = conn.execute("SELECT next_ordinal FROM kb_state WHERE id=1").fetchone()[0]
            conn.execute("UPDATE kb_state SET next_ordinal = next_ordinal + 1 WHERE id=1")
            conn.execute(
                "INSERT INTO techniques (technique_id, ordinal, norm_key, title, body, tags_json, "
                "surfaces_json, source_engagement, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"t_{uuid.uuid4().hex[:12]}", ordinal, norm, title, body, json.dumps(tags),
                 json.dumps(surfaces), source_engagement, time.time()),
            )
            return {"ordinal": ordinal, "deduped": False}

    def recall(self, query: str = "", surface: str | None = None, *, limit: int = 8, bump: bool = True) -> list[dict]:
        q = _norm(query).lstrip("t").lstrip("-")
        surf = (surface or "").strip().lower()
        scored = []
        with self._connect() as conn:
            rows = [self._row(r) for r in conn.execute("SELECT * FROM techniques ORDER BY ordinal").fetchall()]
        for t in rows:
            score = 0
            hay = _norm(f"{t['title']} {t['body']} {' '.join(t['tags'])} {' '.join(t['surfaces'])}")
            if q and q in hay:
                score += 3
            if q:
                score += sum(1 for w in set(q.split()) if len(w) > 3 and w in hay)
            if surf:
                score += sum(3 for s in t["surfaces"] if s.lower() in surf or surf in s.lower())
                score += sum(2 for tag in t["tags"] if tag in surf)
            if not q and not surf:
                score = 1  # "list them all" mode
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: (-x[0], -x[1]["ordinal"]))
        out = [t for _, t in scored[:limit]]
        if bump and out:
            self._bump([t["technique_id"] for t in out])
        return out

    def semantic_recall(self, query: str = "", surface: str | None = None, *, limit: int = 5,
                         min_score: float = 0.12, exclude_engagement: str | None = None,
                         bump: bool = True) -> list[dict]:
        """Rank by TF-IDF cosine similarity (kb_vectors.py) plus the same keyword/surface bonus
        `recall()` uses, so an exact hit still wins ties but a differently-worded query can also
        surface a relevant technique. `exclude_engagement` drops techniques sourced from the
        caller's own engagement — used for auto-surfacing, so a note doesn't echo itself back as
        "from past engagements" the same turn it was written."""
        with self._connect() as conn:
            rows = [self._row(r) for r in conn.execute("SELECT * FROM techniques ORDER BY ordinal").fetchall()]
        if exclude_engagement:
            rows = [t for t in rows if t["source_engagement"] != exclude_engagement]
        if not rows:
            return []
        query_text = " ".join(x for x in [query, surface] if x)
        if not query_text.strip():
            return []
        corpus = {t["technique_id"]: f"{t['title']} {t['body']} {' '.join(t['tags'])} {' '.join(t['surfaces'])}"
                  for t in rows}
        index = TfidfIndex(corpus)
        qvec = index.query_vector(query_text)
        surf = (surface or "").strip().lower()
        scored = []
        for t in rows:
            score = TfidfIndex.cosine(qvec, index.vectors[t["technique_id"]])
            hay = _norm(f"{t['title']} {t['body']} {' '.join(t['tags'])} {' '.join(t['surfaces'])}")
            if query and _norm(query) in hay:
                score += 0.3
            if surf:
                if any(s.lower() in surf or surf in s.lower() for s in t["surfaces"]):
                    score += 0.3
                if any(tag in surf for tag in t["tags"]):
                    score += 0.15
            if score >= min_score:
                scored.append((score, t))
        scored.sort(key=lambda x: (-x[0], -x[1]["ordinal"]))
        out = [t for _, t in scored[:limit]]
        if bump and out:
            self._bump([t["technique_id"] for t in out])
        return out

    def _bump(self, technique_ids: list[str]) -> None:
        with self._connect() as conn:
            for tid in technique_ids:
                conn.execute("UPDATE techniques SET use_count = use_count + 1, last_used_at = ? WHERE technique_id = ?",
                             (time.time(), tid))

    def all(self) -> list[dict]:
        with self._connect() as conn:
            return [self._row(r) for r in conn.execute("SELECT * FROM techniques ORDER BY ordinal DESC").fetchall()]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM techniques").fetchone()[0]

    def delete(self, ref: str | int) -> bool:
        num = str(ref).lower().lstrip("t").lstrip("-")
        with self._connect() as conn:
            if num.isdigit():
                cur = conn.execute("DELETE FROM techniques WHERE ordinal=?", (int(num),))
            else:
                cur = conn.execute("DELETE FROM techniques WHERE technique_id=?", (str(ref),))
            return cur.rowcount > 0
