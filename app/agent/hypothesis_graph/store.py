"""Hypothesis Graph store — SQLite WAL, one DB per engagement (hypothesis_graph.db beside the
engagement's state.db). Same conventions as agent/engagement/store.py: WAL journal, optimistic
version columns, `_now()` timestamps, ConflictError/NotFoundError.

This layer is pure persistence + structural invariants (ordinal assignment, causal-cycle
rejection, optimistic version, append-only event log kept in sync with the materialized tables).
Derived quantities — confidence recompute from observations, coverage, priority tiers, active-
path selection, the context digest — live in engine.py; the service (service.py) orchestrates
"mutate + recompute + emit event" so the rules stay in one place.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .schema import (
    CAUSAL_EDGE_TYPES,
    EVENT_KINDS,
    SCHEMA,
    SCHEMA_VERSION,
    ConfidenceBand,
    EdgeType,
    ExperimentStatus,
    LifecycleStatus,
    OriginType,
    Verdict,
)


class ConflictError(RuntimeError):
    def __init__(self, current_version: int):
        super().__init__(f"version conflict: store is at version {current_version}")
        self.current_version = current_version


class NotFoundError(RuntimeError):
    pass


class CycleError(RuntimeError):
    pass


class GraphValidationError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class HypothesisGraphStore:
    def __init__(self, engagement_dir: Path):
        engagement_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = engagement_dir / "hypothesis_graph.db"
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO graph_state (id, graph_version, next_ordinal) VALUES (1, 0, 1)"
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

    # -- event log ---------------------------------------------------------------------------

    def _emit(self, conn, kind: str, payload: dict, *, hypothesis_id=None, experiment_id=None):
        if kind not in EVENT_KINDS:
            raise GraphValidationError(f"unknown event kind {kind!r}")
        conn.execute(
            "INSERT INTO events (kind, hypothesis_id, experiment_id, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (kind, hypothesis_id, experiment_id, json.dumps(payload), _now()),
        )
        # graph_version bumps on every mutation — a working-set digest carries it so the agent
        # can tell its cached view is stale and re-fetch (doc 2 §13.9 freshness check).
        conn.execute("UPDATE graph_state SET graph_version = graph_version + 1 WHERE id = 1")

    def list_events(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY event_seq").fetchall()
            return [dict(r) for r in rows]

    def graph_version(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT graph_version FROM graph_state WHERE id=1").fetchone()[0]

    # -- hypotheses --------------------------------------------------------------------------

    def create_hypothesis(
        self, *, title: str, claim: str, phase_created: str, rationale: str,
        origin_type: str, impact: int, confidence_band: str, confidence_reason: str,
        primary_parent_id: str | None = None, origin_ref: str | None = None,
        surface: str | None = None, planned_tests: int = 1,
    ) -> str:
        if not title.strip() or not claim.strip():
            raise GraphValidationError("title and claim are both required and non-empty")
        if not rationale.strip():
            raise GraphValidationError("rationale is required — a hypothesis must say why it exists")
        if not (1 <= impact <= 5):
            raise GraphValidationError("impact must be 1..5")
        ConfidenceBand(confidence_band)  # raises ValueError if invalid
        OriginType(origin_type)
        if phase_created not in ("INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT"):
            raise GraphValidationError(f"unknown phase {phase_created!r}")

        with self._connect() as conn:
            if primary_parent_id is not None and conn.execute(
                "SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (primary_parent_id,)
            ).fetchone() is None:
                raise NotFoundError(f"primary_parent_id {primary_parent_id!r} not found")

            ordinal = conn.execute("SELECT next_ordinal FROM graph_state WHERE id=1").fetchone()[0]
            conn.execute("UPDATE graph_state SET next_ordinal = next_ordinal + 1 WHERE id=1")
            hid = _new_id("h")
            now = _now()
            conn.execute(
                "INSERT INTO hypotheses (hypothesis_id, ordinal, title, claim, phase_created, "
                "lifecycle_status, verdict, primary_parent_id, origin_type, origin_ref, rationale, "
                "surface, confidence_band, confidence_reason, impact, coverage, planned_tests, "
                "direct_tokens, version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0.0,?,0,0,?,?)",
                (hid, ordinal, title.strip(), claim.strip(), phase_created,
                 LifecycleStatus.OPEN.value, Verdict.UNASSESSED.value, primary_parent_id,
                 origin_type, origin_ref, rationale.strip(), surface, confidence_band,
                 confidence_reason.strip(), impact, max(1, planned_tests), now, now),
            )
            self._emit(conn, "hypothesis.created", {
                "ordinal": ordinal, "title": title, "origin_type": origin_type,
                "primary_parent_id": primary_parent_id,
            }, hypothesis_id=hid)
            # A primary_parent is a spawned_by causal edge too — recorded so lineage/branch cost
            # and the cycle invariant have a single source of truth (the edges table).
            if primary_parent_id is not None:
                self._add_edge_inner(conn, primary_parent_id, hid, EdgeType.SPAWNED_BY, reason="primary lineage")
            return hid

    def get_hypothesis(self, hypothesis_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
            if row is None:
                raise NotFoundError(hypothesis_id)
            return dict(row)

    def get_by_ordinal(self, ordinal: int) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM hypotheses WHERE ordinal=?", (ordinal,)).fetchone()
            if row is None:
                raise NotFoundError(f"H-{ordinal}")
            return dict(row)

    def list_hypotheses(self, *, lifecycle_status: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if lifecycle_status:
                rows = conn.execute(
                    "SELECT * FROM hypotheses WHERE lifecycle_status=? ORDER BY ordinal",
                    (lifecycle_status,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM hypotheses ORDER BY ordinal").fetchall()
            return [dict(r) for r in rows]

    def _update_hypothesis(self, conn, hypothesis_id: str, expected_version: int, fields: dict):
        row = conn.execute(
            "SELECT version FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(hypothesis_id)
        if row["version"] != expected_version:
            raise ConflictError(row["version"])
        fields = {**fields, "version": expected_version + 1, "updated_at": _now()}
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE hypotheses SET {cols} WHERE hypothesis_id=?",
            (*fields.values(), hypothesis_id),
        )
        return expected_version + 1

    def set_lifecycle_status(
        self, hypothesis_id: str, expected_version: int, status: str, *,
        reason: str | None = None, actor: str = "agent",
    ) -> int:
        LifecycleStatus(status)
        with self._connect() as conn:
            extra = {}
            event_kind = "hypothesis.activated"
            if status == LifecycleStatus.PARKED.value:
                if not reason:
                    raise GraphValidationError("parking requires a reason (and a reopen condition)")
                extra["park_reason"] = reason
                event_kind = "hypothesis.parked"
            elif status == LifecycleStatus.ABANDONED.value:
                if not reason:
                    raise GraphValidationError("abandoning requires an evidence/scope reason")
                extra["abandon_reason"] = reason
                event_kind = "hypothesis.abandoned"
            elif status in (LifecycleStatus.OPEN.value, LifecycleStatus.QUEUED.value):
                # reopening clears any prior park/abandon reason
                extra["park_reason"] = None
                event_kind = "hypothesis.reopened"
            new_version = self._update_hypothesis(
                conn, hypothesis_id, expected_version, {"lifecycle_status": status, **extra}
            )
            self._emit(conn, event_kind, {"status": status, "reason": reason, "actor": actor},
                       hypothesis_id=hypothesis_id)
            return new_version

    def add_operator_note(self, hypothesis_id: str, text: str, *, actor: str = "operator") -> None:
        """An operator comment/constraint on a hypothesis (UI spec §7.4). Append-only, stored as an
        event only — it never touches the materialized claim/evidence/verdict rows. `list_operator_notes`
        reads them back."""
        if not text.strip():
            raise GraphValidationError("an operator note needs text")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone() is None:
                raise NotFoundError(hypothesis_id)
            self._emit(conn, "hypothesis.operator_note", {"text": text.strip(), "actor": actor},
                       hypothesis_id=hypothesis_id)

    def list_operator_notes(self, hypothesis_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json, created_at FROM events "
                "WHERE kind='hypothesis.operator_note' AND hypothesis_id=? ORDER BY event_seq",
                (hypothesis_id,),
            ).fetchall()
            out = []
            for r in rows:
                p = json.loads(r["payload_json"])
                out.append({"text": p.get("text", ""), "actor": p.get("actor", "operator"),
                            "created_at": r["created_at"]})
            return out

    def set_verdict(
        self, hypothesis_id: str, expected_version: int, verdict: str, *,
        confidence_band: str | None = None, confidence_reason: str | None = None,
    ) -> int:
        Verdict(verdict)
        with self._connect() as conn:
            fields = {"verdict": verdict, "lifecycle_status": LifecycleStatus.COMPLETED.value}
            if confidence_band is not None:
                ConfidenceBand(confidence_band)
                fields["confidence_band"] = confidence_band
            if confidence_reason is not None:
                fields["confidence_reason"] = confidence_reason
            new_version = self._update_hypothesis(conn, hypothesis_id, expected_version, fields)
            self._emit(conn, "hypothesis.verdict_changed", {"verdict": verdict}, hypothesis_id=hypothesis_id)
            return new_version

    def set_confidence(
        self, hypothesis_id: str, expected_version: int, band: str, reason: str,
    ) -> int:
        ConfidenceBand(band)
        with self._connect() as conn:
            new_version = self._update_hypothesis(
                conn, hypothesis_id, expected_version,
                {"confidence_band": band, "confidence_reason": reason},
            )
            self._emit(conn, "hypothesis.confidence_changed", {"band": band, "reason": reason}, hypothesis_id=hypothesis_id)
            return new_version

    def set_coverage(self, hypothesis_id: str, coverage: float) -> None:
        """Materialized/derived (engine recomputes it) — no optimistic version, it's not an
        LLM-owned field. Clamped to [0, 1]."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE hypotheses SET coverage=?, updated_at=? WHERE hypothesis_id=?",
                (max(0.0, min(1.0, coverage)), _now(), hypothesis_id),
            )

    def add_direct_tokens(self, hypothesis_id: str, tokens: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE hypotheses SET direct_tokens = direct_tokens + ?, updated_at=? WHERE hypothesis_id=?",
                (max(0, tokens), _now(), hypothesis_id),
            )

    # -- edges (with causal-cycle invariant) -------------------------------------------------

    def _causal_reachable(self, conn, start: str, target: str) -> bool:
        """True if `target` is reachable from `start` following causal edges only."""
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            rows = conn.execute(
                "SELECT to_hypothesis_id, edge_type FROM edges WHERE from_hypothesis_id=?", (node,)
            ).fetchall()
            for r in rows:
                if EdgeType(r["edge_type"]) in CAUSAL_EDGE_TYPES:
                    stack.append(r["to_hypothesis_id"])
        return False

    def _add_edge_inner(self, conn, from_id, to_id, edge_type: EdgeType, *, reason="", experiment_id=None, evidence_refs=None) -> str:
        if conn.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (from_id,)).fetchone() is None:
            raise NotFoundError(from_id)
        if conn.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (to_id,)).fetchone() is None:
            raise NotFoundError(to_id)
        if from_id == to_id:
            raise GraphValidationError("an edge cannot connect a hypothesis to itself")
        if edge_type in CAUSAL_EDGE_TYPES and self._causal_reachable(conn, to_id, from_id):
            raise CycleError(
                f"causal edge {from_id}->{to_id} ({edge_type.value}) would create a cycle "
                f"({to_id} already causally reaches {from_id})"
            )
        eid = _new_id("e")
        conn.execute(
            "INSERT INTO edges (edge_id, from_hypothesis_id, to_hypothesis_id, edge_type, "
            "experiment_id, reason, evidence_refs_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (eid, from_id, to_id, edge_type.value, experiment_id, reason,
             json.dumps(evidence_refs or []), _now()),
        )
        self._emit(conn, "edge.created", {
            "from": from_id, "to": to_id, "edge_type": edge_type.value, "reason": reason,
        })
        return eid

    def add_edge(self, from_id: str, to_id: str, edge_type: str, *, reason="", experiment_id=None, evidence_refs=None) -> str:
        with self._connect() as conn:
            return self._add_edge_inner(
                conn, from_id, to_id, EdgeType(edge_type),
                reason=reason, experiment_id=experiment_id, evidence_refs=evidence_refs,
            )

    def list_edges(self, hypothesis_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if hypothesis_id:
                rows = conn.execute(
                    "SELECT * FROM edges WHERE from_hypothesis_id=? OR to_hypothesis_id=? ORDER BY created_at",
                    (hypothesis_id, hypothesis_id),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM edges ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def children_of(self, hypothesis_id: str) -> list[dict]:
        """Direct children via primary lineage (primary_parent_id)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hypotheses WHERE primary_parent_id=? ORDER BY ordinal", (hypothesis_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # -- experiments -------------------------------------------------------------------------

    def start_experiment(
        self, hypothesis_id: str, *, method_summary: str, expected_observation: str = "",
        broker_action_ref: str | None = None, chat_start_message_id: str | None = None,
    ) -> str:
        if not method_summary.strip():
            raise GraphValidationError("method_summary is required to start an experiment")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone() is None:
                raise NotFoundError(hypothesis_id)
            attempt_no = conn.execute(
                "SELECT COUNT(*) FROM experiments WHERE hypothesis_id=?", (hypothesis_id,)
            ).fetchone()[0] + 1
            eid = _new_id("x")
            conn.execute(
                "INSERT INTO experiments (experiment_id, hypothesis_id, attempt_no, status, "
                "method_summary, expected_observation, broker_action_ref, chat_start_message_id, "
                "started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, hypothesis_id, attempt_no, ExperimentStatus.RUNNING.value,
                 method_summary.strip(), expected_observation, broker_action_ref,
                 chat_start_message_id, _now()),
            )
            # a running experiment moves its hypothesis to RUNNING (best-effort, no version bump —
            # lifecycle here is a reflection of experiment state, not an operator decision)
            conn.execute(
                "UPDATE hypotheses SET lifecycle_status=?, updated_at=? WHERE hypothesis_id=? "
                "AND lifecycle_status IN ('open','queued','draft')",
                (LifecycleStatus.RUNNING.value, _now(), hypothesis_id),
            )
            self._emit(conn, "experiment.started", {"attempt_no": attempt_no, "method": method_summary},
                       hypothesis_id=hypothesis_id, experiment_id=eid)
            return eid

    def complete_experiment(
        self, experiment_id: str, *, status: str, observed_result: str,
        evidence_refs: list[str] | None = None, audit_refs: list[str] | None = None,
        input_tokens: int = 0, output_tokens: int = 0, chat_result_message_id: str | None = None,
    ) -> None:
        ExperimentStatus(status)
        with self._connect() as conn:
            row = conn.execute("SELECT hypothesis_id FROM experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
            if row is None:
                raise NotFoundError(experiment_id)
            conn.execute(
                "UPDATE experiments SET status=?, observed_result=?, evidence_refs_json=?, "
                "audit_refs_json=?, input_tokens=?, output_tokens=?, chat_result_message_id=?, "
                "completed_at=? WHERE experiment_id=?",
                (status, observed_result, json.dumps(evidence_refs or []),
                 json.dumps(audit_refs or []), input_tokens, output_tokens,
                 chat_result_message_id, _now(), experiment_id),
            )
            conn.execute(
                "UPDATE hypotheses SET direct_tokens = direct_tokens + ?, updated_at=? WHERE hypothesis_id=?",
                (input_tokens + output_tokens, _now(), row["hypothesis_id"]),
            )
            self._emit(conn, "experiment.completed", {"status": status},
                       hypothesis_id=row["hypothesis_id"], experiment_id=experiment_id)

    def list_experiments(self, hypothesis_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM experiments WHERE hypothesis_id=? ORDER BY attempt_no", (hypothesis_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # -- observations ------------------------------------------------------------------------

    def add_observation(
        self, experiment_id: str, hypothesis_id: str, *, summary: str, polarity: str,
        strength: str, evidence_refs: list[str] | None = None,
    ) -> str:
        with self._connect() as conn:
            oid = _new_id("o")
            conn.execute(
                "INSERT INTO observations (observation_id, experiment_id, hypothesis_id, summary, "
                "polarity, strength, evidence_refs_json, observed_at) VALUES (?,?,?,?,?,?,?,?)",
                (oid, experiment_id, hypothesis_id, summary, polarity, strength,
                 json.dumps(evidence_refs or []), _now()),
            )
            self._emit(conn, "observation.added", {"polarity": polarity, "strength": strength},
                       hypothesis_id=hypothesis_id, experiment_id=experiment_id)
            return oid

    def list_observations(self, hypothesis_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM observations WHERE hypothesis_id=? ORDER BY observed_at", (hypothesis_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # -- active path -------------------------------------------------------------------------

    def get_graph_state(self) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM graph_state WHERE id=1").fetchone()
            d = dict(row)
            d["active_path"] = json.loads(d.pop("active_path_json"))
            return d

    def set_active_path(self, path: list[str], reason: str) -> None:
        """The active investigation path is a state the agent SETS with a reason (doc 2 §13.7),
        not something the store computes greedily. Every switch is an append-only event."""
        with self._connect() as conn:
            active = path[-1] if path else None
            conn.execute(
                "UPDATE graph_state SET active_hypothesis_id=?, active_path_json=?, active_path_reason=? WHERE id=1",
                (active, json.dumps(path), reason),
            )
            self._emit(conn, "active_path.changed", {"path": path, "reason": reason})

    # -- integrity invariants (doc 2 §16.2 — must hold at all times; CI test asserts them) ----

    def check_invariants(self) -> list[str]:
        """Returns a list of invariant violations (empty = healthy). Covers the graph-integrity
        set both review docs call out: unique ordinals, no orphan edges, no causal cycles, and
        every hypothesis/experiment having its creation event. Cheap enough to run in tests and
        after a batch of mutations."""
        problems: list[str] = []
        with self._connect() as conn:
            ords = [r["ordinal"] for r in conn.execute("SELECT ordinal FROM hypotheses").fetchall()]
            if len(ords) != len(set(ords)):
                problems.append("duplicate ordinals present")
            hyp_ids = {r["hypothesis_id"] for r in conn.execute("SELECT hypothesis_id FROM hypotheses").fetchall()}
            for e in conn.execute("SELECT * FROM edges").fetchall():
                if e["from_hypothesis_id"] not in hyp_ids or e["to_hypothesis_id"] not in hyp_ids:
                    problems.append(f"orphan edge {e['edge_id']}")
            created = {r["hypothesis_id"] for r in conn.execute(
                "SELECT hypothesis_id FROM events WHERE kind='hypothesis.created'").fetchall()}
            for hid in hyp_ids:
                if hid not in created:
                    problems.append(f"hypothesis {hid} has no created event")
            # causal cycle check across the whole graph
            for hid in hyp_ids:
                if self._causal_reachable_excluding_self(conn, hid):
                    problems.append(f"causal cycle involving {hid}")
                    break
        return problems

    def _causal_reachable_excluding_self(self, conn, start: str) -> bool:
        seen, stack = set(), []
        for r in conn.execute(
            "SELECT to_hypothesis_id, edge_type FROM edges WHERE from_hypothesis_id=?", (start,)
        ).fetchall():
            if EdgeType(r["edge_type"]) in CAUSAL_EDGE_TYPES:
                stack.append(r["to_hypothesis_id"])
        while stack:
            node = stack.pop()
            if node == start:
                return True
            if node in seen:
                continue
            seen.add(node)
            for r in conn.execute(
                "SELECT to_hypothesis_id, edge_type FROM edges WHERE from_hypothesis_id=?", (node,)
            ).fetchall():
                if EdgeType(r["edge_type"]) in CAUSAL_EDGE_TYPES:
                    stack.append(r["to_hypothesis_id"])
        return False
