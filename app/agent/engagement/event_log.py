"""The engagement-level event log — one append-only, per-engagement, strictly-sequenced stream.

`AGENT_ARCHITECTURE.md` §14.2 M and `CLIENT_UI_DESIGN.md` §4: today the only event stream is
`/api/sessions/{id}/events`, bound to one session and held in an in-memory queue that vanishes
with the process. A wave is many sessions, and five client surfaces plus the reconnect counter
(§5.5) and the trajectory spine (§8) all need *one* ordered engagement-level stream that
survives a restart and can be replayed from any point. This is that stream.

Why SQLite and not the session queue pattern:

  * **It must persist.** §4.1.3: the log is kept for the life of the engagement, travels in the
    resumable export, and is the scrubber's whole range. An in-memory queue loses all of that on
    the first restart.
  * **It is written from several processes.** Workers run in the security_mcp_server subprocess
    and in AutonomousDriver threads; a browser SSE connection reads from the web process. A
    process-local queue cannot carry a write made in another process. SQLite's own writer lock
    does, and WAL lets the SSE readers poll without blocking the writers.

Why the sequence number is allocated the way it is (`CLIENT_UI_DESIGN.md` §4.1.1):

    The number is assigned by the append, not read before it.

A wave has several workers emitting at once. "Read the last sequence number, add one, write it"
is check-then-act — the identical shape to the broker cooldown race (`broker/broker.py`'s
`_cooldown_lock`) and the spend race (§14.1 D) — and here the failure is *quiet*: two events
share a number, a reconnecting window computes a nonsensical gap, and the scrubber replays a
history that never happened. So `append()` does the read and the write as **one** SQL statement,
`INSERT ... SELECT COALESCE(MAX(seq),0)+1 FROM events`, under a `BEGIN IMMEDIATE` write lock that
SQLite grants to exactly one writer at a time. No caller can observe — or be handed — a number
another caller also gets. Rolled-back appends consume no number, so the sequence stays gapless,
which is what the §5.5 missed-event count is subtracted from.

`test_event_log.py` proves that race is real before trusting the fix: it runs the *wrong*
(check-then-act) pattern under threads and asserts it collides, so the concurrency test is known
not to be inert, then asserts the real `append()` never does.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# The read-model projection (§4.2) folds these kinds into typed state; any other kind is still
# recorded and replayed, it just lands in the raw timeline rather than a typed slice. Keeping the
# set here — rather than scattered across the producers that will emit them over build steps 3–7
# — is what lets the projection and its test name the same vocabulary.
KNOWN_KINDS = (
    "wave_started", "wave_ended",
    "worker_spawned", "worker_finished",
    "experiment_started", "experiment_ended",
    "tool_call_started", "tool_call_finished",
    "model_call",
    "artifact_stored",
    "graph_node_changed",
    "note_added", "finding_recorded",
    "approval_required", "approval_resolved",
    "budget_updated",
)


class EngagementEventLog:
    """One instance per engagement directory; the log lives at `<engagement_dir>/events.db`,
    colocated with that engagement's `roe.json`/`state.db` — the same "state belongs with the
    engagement it describes" rule `EngagementStore` follows.

    Constructing one creates the db file, so callers that must not bring a log into existence on
    a read (a plain GET on an engagement that never emitted) use `open_if_exists()` instead."""

    def __init__(self, engagement_dir: Path):
        engagement_dir.mkdir(parents=True, exist_ok=True)
        self.engagement_dir = engagement_dir
        self.db_path = engagement_dir / "events.db"
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "  seq     INTEGER PRIMARY KEY,"  # no AUTOINCREMENT: see append() for why
                "  ts      REAL NOT NULL,"
                "  kind    TEXT NOT NULL,"
                "  payload TEXT NOT NULL"
                ")"
            )

    @classmethod
    def open_if_exists(cls, engagement_dir: Path) -> "EngagementEventLog | None":
        """The log IFF `events.db` already exists, else None — so a read of an engagement that has
        emitted nothing yet does not create one as a side effect (same guard the hypothesis-graph
        and notebook GET endpoints use)."""
        if not (engagement_dir / "events.db").exists():
            return None
        return cls(engagement_dir)

    @contextmanager
    def _connect(self):
        # timeout is the busy-wait: a second writer blocks here until the first commits rather
        # than failing with "database is locked", which is what makes the single-writer
        # serialization hold under the several concurrent emitters a wave produces.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def append(self, kind: str, payload: dict[str, Any] | None = None) -> int:
        """Append one event; return the sequence number the append assigned it.

        The number is allocated *inside* the INSERT, under a write lock held for exactly one
        writer, so it is never read by one caller and then written by another — the whole point
        of §4.1.1. `BEGIN IMMEDIATE` takes the write lock before the `MAX(seq)` is read, so the
        read and the write are one indivisible operation.
        """
        if not kind or not isinstance(kind, str):
            raise ValueError("an event needs a non-empty string kind")
        body = json.dumps(payload or {})
        ts = time.time()
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")  # claim the single writer slot before reading MAX
            cur = conn.execute(
                "INSERT INTO events (seq, ts, kind, payload) "
                "SELECT COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM events",
                (ts, kind, body),
            )
            seq = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        return seq

    def latest_seq(self) -> int:
        """The highest sequence number appended so far, or 0 for an empty log. This is the number
        a snapshot is taken `at`, and the one a reconnecting client's gap is measured against."""
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
            return int(row["m"])

    def read_since(self, after_seq: int, limit: int | None = None) -> list[dict]:
        """Events with `seq > after_seq`, in order. `after_seq=0` reads the whole log from the
        start, which is how a fresh subscriber with no `Last-Event-ID` begins."""
        q = "SELECT seq, ts, kind, payload FROM events WHERE seq > ? ORDER BY seq"
        params: tuple = (after_seq,)
        if limit is not None:
            q += " LIMIT ?"
            params = (after_seq, int(limit))
        with self._connect() as conn:
            return [self._row_to_event(r) for r in conn.execute(q, params).fetchall()]

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> dict:
        return {"seq": int(r["seq"]), "ts": r["ts"], "kind": r["kind"],
                "payload": json.loads(r["payload"])}

    def snapshot(self, at_seq: int | None = None) -> dict:
        """The read-model projection (§4.2) folded from the log up to and including `at_seq`
        (default: the whole log). `snapshot(at=N)` is exactly `project(read_since(0))` truncated
        at N — the time scrubber (§6.5) *is* this method called with an earlier number, which is
        why it must be a pure fold over the log and never a second data path."""
        latest = self.latest_seq()
        at = latest if at_seq is None else min(int(at_seq), latest)
        events = [e for e in self.read_since(0) if e["seq"] <= at]
        return project(events, at_seq=at, latest_seq=latest)


def project(events: list[dict], *, at_seq: int, latest_seq: int) -> dict:
    """Fold an ordered event list into the current-state read model every surface rebuilds from
    (`CLIENT_UI_DESIGN.md` §3: no window owns state — it holds only a projection it can rebuild
    from the stream). Pure function of its inputs, so a window opened three hours apart from
    another, or run to an earlier `at_seq` by the scrubber, computes identical content.

    The typed slices follow §4.2's table. Unknown kinds are not dropped — they are counted and
    kept on the raw timeline tail — so a producer that emits a kind this projection has not
    learned yet still appears in the stream and the resume count, rather than silently vanishing.
    """
    waves: dict[str, dict] = {}
    workers: dict[str, dict] = {}
    experiments: dict[str, dict] = {}
    tool_calls: dict[str, dict] = {}          # in-flight tool calls, keyed by call id
    model_roster: dict[str, dict] = {}        # keyed by model id
    artifacts: list[dict] = []
    graph_nodes: dict[str, dict] = {}         # keyed by node/ordinal
    approvals: dict[str, dict] = {}           # keyed by request id; pending + resolved
    budget: dict = {}
    counts = {"note_added": 0, "finding_recorded": 0}
    unknown: list[dict] = []

    for e in events:
        kind, p = e["kind"], e["payload"]
        if kind == "wave_started":
            waves[str(p.get("wave"))] = {"wave": p.get("wave"), "status": "running",
                                         "experiments": p.get("experiments", []),
                                         "started_seq": e["seq"]}
        elif kind == "wave_ended":
            waves.setdefault(str(p.get("wave")), {"wave": p.get("wave")})
            waves[str(p.get("wave"))].update(status="ended", ended_seq=e["seq"])
        elif kind == "worker_spawned":
            workers[str(p.get("worker_id"))] = {"worker_id": p.get("worker_id"),
                                                "hypothesis": p.get("hypothesis"),
                                                "method": p.get("method"), "status": "running",
                                                "spawned_seq": e["seq"]}
        elif kind == "worker_finished":
            w = workers.setdefault(str(p.get("worker_id")), {"worker_id": p.get("worker_id")})
            w.update(status="finished", outcome=p.get("outcome"), finished_seq=e["seq"])
        elif kind == "experiment_started":
            experiments[str(p.get("hypothesis_id")) + "/" + str(p.get("attempt"))] = {
                "hypothesis_id": p.get("hypothesis_id"), "attempt": p.get("attempt"),
                "status": "running", "started_seq": e["seq"]}
        elif kind == "experiment_ended":
            k = str(p.get("hypothesis_id")) + "/" + str(p.get("attempt"))
            experiments.setdefault(k, {"hypothesis_id": p.get("hypothesis_id"),
                                       "attempt": p.get("attempt")})
            experiments[k].update(status="ended", verdict=p.get("verdict"), ended_seq=e["seq"])
        elif kind == "tool_call_started":
            tool_calls[str(p.get("call_id"))] = {"call_id": p.get("call_id"),
                                                 "worker": p.get("worker"), "tool": p.get("tool"),
                                                 "argument_digest": p.get("argument_digest"),
                                                 "started_seq": e["seq"]}
        elif kind == "tool_call_finished":
            # In-flight set only: a finished call is removed, so what remains is "running now",
            # which is what the flow view and rail draw. The full history stays on the log.
            tool_calls.pop(str(p.get("call_id")), None)
        elif kind == "model_call":
            mid = str(p.get("model_id"))
            m = model_roster.setdefault(mid, {"model_id": p.get("model_id"), "calls": 0,
                                              "prompt_tokens": 0, "completion_tokens": 0,
                                              "cost": 0.0})
            m["calls"] += 1
            m["prompt_tokens"] += int(p.get("prompt_tokens", 0) or 0)
            m["completion_tokens"] += int(p.get("completion_tokens", 0) or 0)
            m["cost"] += float(p.get("cost", 0.0) or 0.0)
            if p.get("role"):
                m["role"] = p.get("role")
        elif kind == "artifact_stored":
            # §4.2/§6.3.1: where a worker's output went. Kept as a list (not collapsed) because
            # the flow view draws an edge per artifact and "a node whose output goes nowhere" is
            # the signal — collapsing would erase exactly the thing §6.3.1 says not to simplify.
            artifacts.append({"seq": e["seq"], "worker": p.get("worker"),
                              "artifact": p.get("artifact"), "store": p.get("store"),
                              "ref": p.get("ref")})
        elif kind == "graph_node_changed":
            graph_nodes[str(p.get("node"))] = {"node": p.get("node"), "status": p.get("status"),
                                               "verdict": p.get("verdict"), "seq": e["seq"]}
        elif kind == "note_added":
            counts["note_added"] += 1
        elif kind == "finding_recorded":
            counts["finding_recorded"] += 1
        elif kind == "approval_required":
            approvals[str(p.get("request_id"))] = {"request_id": p.get("request_id"),
                                                   "request": p.get("request"),
                                                   "blocked_workers": p.get("blocked_workers", []),
                                                   "status": "pending", "seq": e["seq"]}
        elif kind == "approval_resolved":
            a = approvals.setdefault(str(p.get("request_id")),
                                     {"request_id": p.get("request_id")})
            a.update(status=p.get("status", "resolved"), resolved_seq=e["seq"])
        elif kind == "budget_updated":
            budget = {"spent": p.get("spent"), "reserved": p.get("reserved"),
                      "remaining": p.get("remaining"), "seq": e["seq"]}
        else:
            unknown.append(e)

    return {
        "at_seq": at_seq,
        "latest_seq": latest_seq,
        "waves": list(waves.values()),
        "workers": list(workers.values()),
        "experiments": list(experiments.values()),
        "tool_calls_in_flight": list(tool_calls.values()),
        "model_roster": list(model_roster.values()),
        "artifacts": artifacts,
        "graph_nodes": list(graph_nodes.values()),
        "approvals": list(approvals.values()),
        "budget": budget,
        "counts": counts,
        "unknown_events": unknown,
    }
