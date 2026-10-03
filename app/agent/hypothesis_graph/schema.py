"""Hypothesis Graph — MVP 0 semantics and storage schema.

Named "Hypothesis Tree" in the UI (easy to grasp), but a **typed DAG** internally, per the
2026-09-01 feature-design doc: the original "tree" framing breaks because one experiment result
can synthesize several source hypotheses (a node with multiple inbound edges), and one claim can
be tested many times. So the model separates four things the flat EngagementStore.hypotheses
table conflated:

  - Hypothesis  — a falsifiable claim. Has ONE primary_parent (for layout + primary-branch cost)
                  but can have many inbound edges of other types (provenance / synthesis).
  - Edge        — a typed, directed relationship between hypotheses (spawned_by, supports,
                  contradicts, synthesized_from, ...). Causal edges must stay acyclic.
  - Experiment  — one attempt to test a hypothesis. A hypothesis can have many. This is what
                  actually touches a target (and so carries a broker_action_ref), NOT the
                  hypothesis mutation itself.
  - Observation — an interpreted result of an experiment (supports/refutes/neutral, a strength),
                  pointing at immutable evidence-store refs rather than copying raw output.

Three separate quantities are kept distinct on purpose (the single most important correction in
both review docs — conflating them produces false precision and wrong agent decisions):
  - confidence  — how likely the CLAIM is true. Moved by evidence for/against, never by "did a
                  lot of work". Bands (low/medium/high), not a fake probability percentage.
  - coverage    — fraction of planned tests that have a verdict. Progress, not truth.
  - priority    — worth-doing-next. A ranking heuristic over impact / info-gain / feasibility /
                  *remaining* cost / risk — never over sunk (already-spent) tokens.

Mutations go through a HypothesisGraphService (a state service that validates schema, version,
provenance and writes an append-only event), NOT through the execution broker — the broker
governs actions against a target under RoE, which is a different authority. Only the experiment's
own target-touching tool calls go through the broker, and the experiment records that
broker_action_ref to tie reasoning to authorization.
"""
from __future__ import annotations

from enum import Enum


class LifecycleStatus(str, Enum):
    """Where a hypothesis is in its workflow — orthogonal to whether its claim is true (Verdict).
    'ทดลองแล้ว = สีเข้ม' from the original spec is only `completed` here; the rest are the states
    both review docs said the two-state (tested/not) model was missing."""
    DRAFT = "draft"
    OPEN = "open"
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    PARKED = "parked"       # the reversible default for "not worth it right now" — NOT abandoned
    ABANDONED = "abandoned"  # out of scope / RoE conflict / duplicate / untestable — with a reason


class Verdict(str, Enum):
    UNASSESSED = "unassessed"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    CONFIRMED = "confirmed"     # requires a verification rule + evidence class, not just a guess
    SUPERSEDED = "superseded"   # replaced by a newer hypothesis (points via a supersedes edge)


class EdgeType(str, Enum):
    SPAWNED_BY = "spawned_by"            # an attempt result produced this new hypothesis
    REFINES = "refines"                 # narrows/sharpens a parent claim
    SUPPORTS = "supports"               # source gives evidence for target
    CONTRADICTS = "contradicts"         # source conflicts with target
    SYNTHESIZED_FROM = "synthesized_from"  # this claim combines several source hypotheses
    REFERENCES = "references"           # related but not causal
    SUPERSEDES = "supersedes"           # this hypothesis replaces an older one


# Edge types that form the causal lineage the DAG must keep acyclic. `references` is associative
# (a cross-link), so it is deliberately excluded from the cycle check.
CAUSAL_EDGE_TYPES = frozenset({
    EdgeType.SPAWNED_BY, EdgeType.REFINES, EdgeType.SYNTHESIZED_FROM, EdgeType.SUPERSEDES,
})


class OriginType(str, Enum):
    AI_INFERENCE = "ai_inference"
    USER_MESSAGE = "user_message"
    TOOL_OBSERVATION = "tool_observation"
    COMBINED_ANALYSIS = "combined_analysis"
    RETEST = "retest"
    IMPORTED_MANUAL_FINDING = "imported_manual_finding"


class ConfidenceBand(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExperimentStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class Polarity(str, Enum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    NEUTRAL = "neutral"


class Strength(str, Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class PriorityTier(str, Enum):
    """MVP surfaces priority as a tier + reasons, not a decimal — avoids illusion of precision
    (both docs). A normalized numeric score exists only for intra-tier sort (engine.py)."""
    NOW = "now"
    NEXT = "next"
    LATER = "later"
    PARKED = "parked"


PHASES = ("INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT")

# Append-only event kinds (doc 2 §11.5) — the graph is rebuildable by replaying these in order.
EVENT_KINDS = (
    "hypothesis.created", "hypothesis.queued", "hypothesis.activated",
    "experiment.started", "experiment.completed",
    "hypothesis.verdict_changed", "hypothesis.confidence_changed",
    "edge.created", "chat_anchor.added",
    "hypothesis.superseded", "hypothesis.parked", "hypothesis.reopened",
    "hypothesis.abandoned", "observation.added", "active_path.changed",
    "hypothesis.operator_note",  # operator comment/constraint (UI spec §7.4) — append-only, never
                                 # touches claim/evidence/verdict history
)


# One executescript() at store init. Materialized tables (hypotheses/edges/experiments/
# observations/graph_state) are the fast read model; the events table is the append-only source
# of truth they can be rebuilt from (store.rebuild_from_events() + a test proves they match).
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    event_seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    hypothesis_id   TEXT,
    experiment_id   TEXT,
    payload_json    TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id     TEXT PRIMARY KEY,
    ordinal           INTEGER NOT NULL,
    title             TEXT NOT NULL,
    claim             TEXT NOT NULL,
    phase_created     TEXT NOT NULL,
    lifecycle_status  TEXT NOT NULL,
    verdict           TEXT NOT NULL,
    primary_parent_id TEXT,
    origin_type       TEXT NOT NULL,
    origin_ref        TEXT,
    rationale         TEXT NOT NULL,
    surface           TEXT,
    confidence_band   TEXT NOT NULL,
    confidence_reason TEXT NOT NULL,
    impact            INTEGER NOT NULL,
    coverage          REAL NOT NULL DEFAULT 0.0,
    planned_tests     INTEGER NOT NULL DEFAULT 1,
    direct_tokens     INTEGER NOT NULL DEFAULT 0,
    abandon_reason    TEXT,
    park_reason       TEXT,
    version           INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    FOREIGN KEY (primary_parent_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id            TEXT PRIMARY KEY,
    from_hypothesis_id TEXT NOT NULL,
    to_hypothesis_id   TEXT NOT NULL,
    edge_type          TEXT NOT NULL,
    experiment_id      TEXT,
    reason             TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at         REAL NOT NULL,
    FOREIGN KEY (from_hypothesis_id) REFERENCES hypotheses(hypothesis_id),
    FOREIGN KEY (to_hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id         TEXT PRIMARY KEY,
    hypothesis_id         TEXT NOT NULL,
    attempt_no            INTEGER NOT NULL,
    status                TEXT NOT NULL,
    method_summary        TEXT NOT NULL,
    expected_observation  TEXT NOT NULL DEFAULT '',
    observed_result       TEXT,
    broker_action_ref     TEXT,
    audit_refs_json       TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json    TEXT NOT NULL DEFAULT '[]',
    chat_start_message_id TEXT,
    chat_result_message_id TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    started_at            REAL,
    completed_at          REAL,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id       TEXT PRIMARY KEY,
    experiment_id        TEXT NOT NULL,
    hypothesis_id        TEXT NOT NULL,
    summary              TEXT NOT NULL,
    polarity             TEXT NOT NULL,
    strength             TEXT NOT NULL,
    evidence_refs_json   TEXT NOT NULL DEFAULT '[]',
    observed_at          REAL NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id),
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE TABLE IF NOT EXISTS graph_state (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    graph_version          INTEGER NOT NULL DEFAULT 0,
    active_hypothesis_id   TEXT,
    active_path_json       TEXT NOT NULL DEFAULT '[]',
    active_path_reason     TEXT NOT NULL DEFAULT '',
    next_ordinal           INTEGER NOT NULL DEFAULT 1
);
"""

SCHEMA_VERSION = 1
