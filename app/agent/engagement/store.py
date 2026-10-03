"""M5.2 — authoritative engagement-state store. Every reviewer independently ranked the lack
of this the single biggest structural gap (STATUS.md's "Asset/target-state store" and
"Hypothesis/coverage/observation store" rows, both citing HackerAI and Codex). Without it the
model re-derives what it already found every turn, from raw tool output, under context pressure
— structure compensates for judgment-under-ambiguity the way the ROADMAP's Phase 5 intro says it
should.

One SQLite database per engagement, colocated with that engagement's `roe.json`/`scope.txt`/
`deny.txt` at `engagements/<id>/state.db` — state belongs with the engagement it describes, not
in one global database mixing engagements together. Same WAL/foreign-keys/Row pattern and the
same optimistic-concurrency shape as `agent/memory_service/db.py` (reused deliberately, per the
ROADMAP's own instruction not to reinvent it) — applied here to the three things actually likely
to race under an approval workflow: phase transitions, hypothesis status, and task status.

This store does NOT duplicate the finding schema (`agent/findings/model.py`) or the evidence
blobs (`agent/evidence/store.py`) — `findings` here is a thin lifecycle/index table pointing at
finding IDs that live in FINDINGS_DIR, and `observations`/anything evidentiary carries an
`evidence_ref` (an evidence-store content ID) rather than a copy of the content itself.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

PHASES = ("INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT")
HYPOTHESIS_STATUSES = ("open", "testing", "confirmed", "refuted", "abandoned")
TASK_STATUSES = ("pending", "running", "blocked", "done", "failed")
FINDING_LIFECYCLE_STATUSES = ("draft", "validated", "reported", "retested", "closed")
COVERAGE_STATUSES = ("not_started", "in_progress", "covered", "skipped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id     TEXT PRIMARY KEY,
    asset_type   TEXT NOT NULL,
    identifier   TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    metadata_json TEXT,
    UNIQUE(asset_type, identifier)
);

CREATE TABLE IF NOT EXISTS services (
    service_id   TEXT PRIMARY KEY,
    asset_id     TEXT NOT NULL,
    port         INTEGER NOT NULL,
    protocol     TEXT NOT NULL,
    service_name TEXT,
    service_version TEXT,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    metadata_json TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
    UNIQUE(asset_id, port, protocol)
);

CREATE TABLE IF NOT EXISTS endpoints (
    endpoint_id  TEXT PRIMARY KEY,
    asset_id     TEXT NOT NULL,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    metadata_json TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
    UNIQUE(asset_id, method, path)
);

CREATE TABLE IF NOT EXISTS relationships (
    relationship_id TEXT PRIMARY KEY,
    from_type    TEXT NOT NULL,
    from_id      TEXT NOT NULL,
    to_type      TEXT NOT NULL,
    to_id        TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    created_at   REAL NOT NULL,
    UNIQUE(from_type, from_id, to_type, to_id, relationship_type)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id   TEXT PRIMARY KEY,
    observation_type TEXT NOT NULL,
    content          TEXT NOT NULL,
    evidence_ref     TEXT,
    asset_id         TEXT,
    endpoint_id      TEXT,
    service_id       TEXT,
    source           TEXT NOT NULL,
    created_at       REAL NOT NULL,
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
    FOREIGN KEY (endpoint_id) REFERENCES endpoints(endpoint_id),
    FOREIGN KEY (service_id) REFERENCES services(service_id)
);
CREATE INDEX IF NOT EXISTS idx_observations_asset ON observations(asset_id);

CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id     TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL,
    priority          TEXT NOT NULL DEFAULT 'medium',
    status            TEXT NOT NULL DEFAULT 'open',
    preconditions     TEXT,
    evidence_for_json     TEXT NOT NULL DEFAULT '[]',
    evidence_against_json TEXT NOT NULL DEFAULT '[]',
    coverage_surface  TEXT,
    owner             TEXT,
    version           INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_ledger (
    coverage_id  TEXT PRIMARY KEY,
    surface      TEXT NOT NULL,
    technique    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'not_started',
    notes        TEXT,
    updated_at   REAL NOT NULL,
    UNIQUE(surface, technique)
);

CREATE TABLE IF NOT EXISTS phase_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    current_phase TEXT NOT NULL DEFAULT 'INTAKE',
    version      INTEGER NOT NULL DEFAULT 0,
    entered_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS phase_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    phase        TEXT NOT NULL,
    entered_at   REAL NOT NULL,
    exited_at    REAL,
    exit_reason  TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    phase        TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    params_json  TEXT,
    result_json  TEXT,
    version      INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS findings_lifecycle (
    finding_id   TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'draft',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS retests (
    retest_id    TEXT PRIMARY KEY,
    finding_id   TEXT NOT NULL,
    performed_at REAL NOT NULL,
    result       TEXT NOT NULL,
    evidence_ref TEXT,
    recipe_ref   TEXT,
    FOREIGN KEY (finding_id) REFERENCES findings_lifecycle(finding_id)
);

CREATE TABLE IF NOT EXISTS closeout (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    closed_at    REAL NOT NULL,
    closed_by    TEXT NOT NULL,
    summary      TEXT NOT NULL,
    findings_closed_count INTEGER NOT NULL,
    outstanding_count     INTEGER NOT NULL
);
"""


class ConflictError(RuntimeError):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"version conflict: current version is {current_version}")


class NotFoundError(RuntimeError):
    pass


class InvalidTransitionError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


class EngagementStore:
    """One instance per engagement directory — `db_path` defaults to
    `engagement_dir/state.db`, matching where M5.1's intake already writes roe/scope/deny."""

    def __init__(self, engagement_dir: Path):
        engagement_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = engagement_dir / "state.db"
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO phase_state (id, current_phase, version, entered_at) "
                "VALUES (1, 'INTAKE', 0, ?)",
                (_now(),),
            )
            row = conn.execute("SELECT COUNT(*) AS n FROM phase_history").fetchone()
            if row["n"] == 0:
                conn.execute(
                    "INSERT INTO phase_history (phase, entered_at) VALUES ('INTAKE', ?)", (_now(),)
                )

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

    # -- assets / services / endpoints / relationships (target model) -----------------------

    def upsert_asset(self, asset_type: str, identifier: str, metadata: dict | None = None) -> str:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT asset_id FROM assets WHERE asset_type=? AND identifier=?",
                (asset_type, identifier),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE assets SET last_seen=?, metadata_json=COALESCE(?, metadata_json) "
                    "WHERE asset_id=?",
                    (now, json.dumps(metadata) if metadata else None, row["asset_id"]),
                )
                return row["asset_id"]
            asset_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO assets (asset_id, asset_type, identifier, first_seen, last_seen, "
                "metadata_json) VALUES (?,?,?,?,?,?)",
                (asset_id, asset_type, identifier, now, now, json.dumps(metadata) if metadata else None),
            )
            return asset_id

    def list_assets(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM assets ORDER BY first_seen").fetchall()]

    def upsert_service(
        self, asset_id: str, port: int, protocol: str, service_name: str | None = None,
        service_version: str | None = None, metadata: dict | None = None,
    ) -> str:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT service_id FROM services WHERE asset_id=? AND port=? AND protocol=?",
                (asset_id, port, protocol),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE services SET last_seen=?, service_name=COALESCE(?, service_name), "
                    "service_version=COALESCE(?, service_version) WHERE service_id=?",
                    (now, service_name, service_version, row["service_id"]),
                )
                return row["service_id"]
            service_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO services (service_id, asset_id, port, protocol, service_name, "
                "service_version, first_seen, last_seen, metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (service_id, asset_id, port, protocol, service_name, service_version, now, now,
                 json.dumps(metadata) if metadata else None),
            )
            return service_id

    def list_services(self, asset_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if asset_id:
                rows = conn.execute("SELECT * FROM services WHERE asset_id=?", (asset_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM services").fetchall()
            return [dict(r) for r in rows]

    def upsert_endpoint(
        self, asset_id: str, method: str, path: str, metadata: dict | None = None
    ) -> str:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT endpoint_id FROM endpoints WHERE asset_id=? AND method=? AND path=?",
                (asset_id, method, path),
            ).fetchone()
            if row:
                conn.execute("UPDATE endpoints SET last_seen=? WHERE endpoint_id=?", (now, row["endpoint_id"]))
                return row["endpoint_id"]
            endpoint_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO endpoints (endpoint_id, asset_id, method, path, first_seen, "
                "last_seen, metadata_json) VALUES (?,?,?,?,?,?,?)",
                (endpoint_id, asset_id, method, path, now, now, json.dumps(metadata) if metadata else None),
            )
            return endpoint_id

    def list_endpoints(self, asset_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if asset_id:
                rows = conn.execute("SELECT * FROM endpoints WHERE asset_id=?", (asset_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM endpoints").fetchall()
            return [dict(r) for r in rows]

    def add_relationship(self, from_type: str, from_id: str, to_type: str, to_id: str, relationship_type: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT relationship_id FROM relationships WHERE from_type=? AND from_id=? "
                "AND to_type=? AND to_id=? AND relationship_type=?",
                (from_type, from_id, to_type, to_id, relationship_type),
            ).fetchone()
            if row:
                return row["relationship_id"]
            relationship_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO relationships (relationship_id, from_type, from_id, to_type, to_id, "
                "relationship_type, created_at) VALUES (?,?,?,?,?,?,?)",
                (relationship_id, from_type, from_id, to_type, to_id, relationship_type, _now()),
            )
            return relationship_id

    # -- observations -------------------------------------------------------------------------

    def add_observation(
        self, observation_type: str, content: str, source: str, *, evidence_ref: str | None = None,
        asset_id: str | None = None, endpoint_id: str | None = None, service_id: str | None = None,
    ) -> str:
        observation_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO observations (observation_id, observation_type, content, "
                "evidence_ref, asset_id, endpoint_id, service_id, source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (observation_id, observation_type, content, evidence_ref, asset_id, endpoint_id,
                 service_id, source, _now()),
            )
        return observation_id

    def list_observations(self, asset_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if asset_id:
                rows = conn.execute(
                    "SELECT * FROM observations WHERE asset_id=? ORDER BY created_at", (asset_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM observations ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    # -- hypotheses (optimistic concurrency) ---------------------------------------------------

    def create_hypothesis(
        self, title: str, description: str, *, priority: str = "medium",
        preconditions: str = "", coverage_surface: str = "", owner: str = "",
    ) -> str:
        hypothesis_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO hypotheses (hypothesis_id, title, description, priority, status, "
                "preconditions, coverage_surface, owner, version, created_at, updated_at) "
                "VALUES (?,?,?,?,'open',?,?,?,0,?,?)",
                (hypothesis_id, title, description, priority, preconditions, coverage_surface,
                 owner, now, now),
            )
        return hypothesis_id

    def get_hypothesis(self, hypothesis_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(hypothesis_id)
            d = dict(row)
            d["evidence_for"] = json.loads(d.pop("evidence_for_json"))
            d["evidence_against"] = json.loads(d.pop("evidence_against_json"))
            return d

    def list_hypotheses(self, status: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM hypotheses WHERE status=?", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM hypotheses").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["evidence_for"] = json.loads(d.pop("evidence_for_json"))
                d["evidence_against"] = json.loads(d.pop("evidence_against_json"))
                out.append(d)
            return out

    def update_hypothesis(
        self, hypothesis_id: str, expected_version: int, *, status: str | None = None,
        add_evidence_for: str | None = None, add_evidence_against: str | None = None,
    ) -> int:
        if status is not None and status not in HYPOTHESIS_STATUSES:
            raise ValueError(f"status must be one of {HYPOTHESIS_STATUSES}, got {status!r}")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version, status, evidence_for_json, evidence_against_json "
                "FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(hypothesis_id)
            if row["version"] != expected_version:
                raise ConflictError(row["version"])

            new_status = status or row["status"]
            evidence_for = json.loads(row["evidence_for_json"])
            evidence_against = json.loads(row["evidence_against_json"])
            if add_evidence_for:
                evidence_for.append(add_evidence_for)
            if add_evidence_against:
                evidence_against.append(add_evidence_against)

            new_version = row["version"] + 1
            conn.execute(
                "UPDATE hypotheses SET status=?, evidence_for_json=?, evidence_against_json=?, "
                "version=?, updated_at=? WHERE hypothesis_id=?",
                (new_status, json.dumps(evidence_for), json.dumps(evidence_against), new_version,
                 _now(), hypothesis_id),
            )
            return new_version

    # -- coverage ledger ------------------------------------------------------------------------

    def set_coverage(self, surface: str, technique: str, status: str, notes: str = "") -> str:
        if status not in COVERAGE_STATUSES:
            raise ValueError(f"status must be one of {COVERAGE_STATUSES}, got {status!r}")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT coverage_id FROM coverage_ledger WHERE surface=? AND technique=?",
                (surface, technique),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE coverage_ledger SET status=?, notes=?, updated_at=? WHERE coverage_id=?",
                    (status, notes, _now(), row["coverage_id"]),
                )
                return row["coverage_id"]
            coverage_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO coverage_ledger (coverage_id, surface, technique, status, notes, "
                "updated_at) VALUES (?,?,?,?,?,?)",
                (coverage_id, surface, technique, status, notes, _now()),
            )
            return coverage_id

    def coverage_summary(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM coverage_ledger GROUP BY status"
            ).fetchall()
            summary = {s: 0 for s in COVERAGE_STATUSES}
            summary.update({r["status"]: r["n"] for r in rows})
            return summary

    # -- phase state (optimistic concurrency + history) ------------------------------------------

    def get_phase(self) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM phase_state WHERE id=1").fetchone()
            return dict(row)

    def transition_phase(self, new_phase: str, reason: str, expected_version: int) -> int:
        if new_phase not in PHASES:
            raise ValueError(f"new_phase must be one of {PHASES}, got {new_phase!r}")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM phase_state WHERE id=1").fetchone()
            if row["version"] != expected_version:
                raise ConflictError(row["version"])
            current_idx = PHASES.index(row["current_phase"])
            new_idx = PHASES.index(new_phase)
            if new_idx < current_idx:
                raise InvalidTransitionError(
                    f"cannot move backward from {row['current_phase']} to {new_phase} — phases "
                    "are forward-only; a re-scoped engagement is a new engagement"
                )
            now = _now()
            new_version = row["version"] + 1
            conn.execute(
                "UPDATE phase_state SET current_phase=?, version=?, entered_at=? WHERE id=1",
                (new_phase, new_version, now),
            )
            conn.execute(
                "UPDATE phase_history SET exited_at=?, exit_reason=? "
                "WHERE phase=? AND exited_at IS NULL",
                (now, reason, row["current_phase"]),
            )
            conn.execute("INSERT INTO phase_history (phase, entered_at) VALUES (?,?)", (new_phase, now))
            return new_version

    def phase_history(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM phase_history ORDER BY id").fetchall()]

    # -- tasks (optimistic concurrency) ----------------------------------------------------------

    def create_task(self, phase: str, task_type: str, params: dict | None = None) -> str:
        task_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, phase, task_type, status, params_json, version, "
                "created_at, updated_at) VALUES (?,?,?,'pending',?,0,?,?)",
                (task_id, phase, task_type, json.dumps(params) if params else None, now, now),
            )
        return task_id

    def update_task(self, task_id: str, expected_version: int, *, status: str, result: dict | None = None) -> int:
        if status not in TASK_STATUSES:
            raise ValueError(f"status must be one of {TASK_STATUSES}, got {status!r}")
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFoundError(task_id)
            if row["version"] != expected_version:
                raise ConflictError(row["version"])
            new_version = row["version"] + 1
            conn.execute(
                "UPDATE tasks SET status=?, result_json=COALESCE(?, result_json), version=?, "
                "updated_at=? WHERE task_id=?",
                (status, json.dumps(result) if result is not None else None, new_version, _now(), task_id),
            )
            return new_version

    def list_tasks(self, phase: str | None = None, status: str | None = None) -> list[dict]:
        with self._connect() as conn:
            query = "SELECT * FROM tasks WHERE 1=1"
            params: list = []
            if phase:
                query += " AND phase=?"
                params.append(phase)
            if status:
                query += " AND status=?"
                params.append(status)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # -- findings lifecycle / retest / closeout ----------------------------------------------------

    def link_finding(self, finding_id: str, status: str = "draft") -> None:
        if status not in FINDING_LIFECYCLE_STATUSES:
            raise ValueError(f"status must be one of {FINDING_LIFECYCLE_STATUSES}, got {status!r}")
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO findings_lifecycle (finding_id, status, created_at, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(finding_id) DO UPDATE SET status=excluded.status, "
                "updated_at=excluded.updated_at",
                (finding_id, status, now, now),
            )

    def update_finding_status(self, finding_id: str, status: str) -> None:
        if status not in FINDING_LIFECYCLE_STATUSES:
            raise ValueError(f"status must be one of {FINDING_LIFECYCLE_STATUSES}, got {status!r}")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT finding_id FROM findings_lifecycle WHERE finding_id=?", (finding_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(finding_id)
            conn.execute(
                "UPDATE findings_lifecycle SET status=?, updated_at=? WHERE finding_id=?",
                (status, _now(), finding_id),
            )

    def list_findings_lifecycle(self, status: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM findings_lifecycle WHERE status=?", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM findings_lifecycle").fetchall()
            return [dict(r) for r in rows]

    def record_retest(
        self, finding_id: str, result: str, *, evidence_ref: str | None = None, recipe_ref: str | None = None
    ) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT finding_id FROM findings_lifecycle WHERE finding_id=?", (finding_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(finding_id)
            retest_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO retests (retest_id, finding_id, performed_at, result, evidence_ref, "
                "recipe_ref) VALUES (?,?,?,?,?,?)",
                (retest_id, finding_id, _now(), result, evidence_ref, recipe_ref),
            )
            conn.execute(
                "UPDATE findings_lifecycle SET status='retested', updated_at=? WHERE finding_id=?",
                (_now(), finding_id),
            )
            return retest_id

    def list_retests(self, finding_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM retests WHERE finding_id=? ORDER BY performed_at", (finding_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def close_engagement(self, closed_by: str, summary: str) -> dict:
        """Mandatory closeout (Phase 6 requirement, recorded here since it's engagement state):
        every finding not already 'closed' is treated as outstanding and counted, never silently
        dropped."""
        with self._connect() as conn:
            existing = conn.execute("SELECT id FROM closeout WHERE id=1").fetchone()
            if existing:
                raise RuntimeError("engagement already closed out — closeout is write-once")
            all_findings = conn.execute("SELECT status FROM findings_lifecycle").fetchall()
            closed = sum(1 for r in all_findings if r["status"] == "closed")
            outstanding = len(all_findings) - closed
            now = _now()
            conn.execute(
                "INSERT INTO closeout (id, closed_at, closed_by, summary, findings_closed_count, "
                "outstanding_count) VALUES (1,?,?,?,?,?)",
                (now, closed_by, summary, closed, outstanding),
            )
            return {
                "closed_at": now, "closed_by": closed_by, "summary": summary,
                "findings_closed_count": closed, "outstanding_count": outstanding,
            }

    def get_closeout(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM closeout WHERE id=1").fetchone()
            return dict(row) if row else None
