"""Per-engagement execution lock — serializes whole `AgentLoop.run_task()` / `AutonomousDriver.run()`
calls against the same engagement's shared, structured state (hypothesis_graph.db, notebook.db,
state.db), not just individual writes to it.

Found live 2026-09-05: two sessions racing against the same engagement (`lab-default`) produced a
duplicate hypothesis — the second session's every-turn digest was built (and shown to the model)
*before* the first session's hypotheses had landed in the graph, so the model had no way to know
they already existed. `hypothesis_graph/store.py`'s optimistic-concurrency version check (every
`_update_hypothesis` call) doesn't prevent this class of bug — that guards against a *lost update*
on one existing record; this was two independent runs each *deciding*, from a stale read, to add a
brand-new one. The only correct fix serializes whole runs against one engagement, not individual
writes: if a second run can't start reading/reasoning about an engagement's state until the first
one has fully finished writing to it, the digest it sees is never stale.

Deliberately coarse (one lock per engagement_id, held for an entire run) rather than fine-grained
per-store locking — the whole point of a shared engagement is a single coherent narrative, and this
box's llama-server already only serves one generation at a time (`--parallel 1`), so two concurrent
runs against the same engagement were never actually running *faster* in parallel, only racing.
This makes that existing serialization deterministic and correct instead of accidentally racy.

Process-local only (a plain lock per engagement_id, not a file lock) — correct for the one
long-lived web-server process every session runs inside; a separate OS process (e.g. the CLI)
touching the same engagement concurrently is a pre-existing, different gap this doesn't address.

**Must be an `RLock`, not a plain `Lock`.** `AutonomousDriver.run()` holds this lock for its
entire run — and, on the very same thread, internally constructs an `AgentLoop` and calls its
own `run_task()` for judgment tasks (`_run_model_task`), which *also* acquires this same
engagement's lock. A plain `Lock` self-deadlocks the instant an autonomous run reaches its first
judgment task; an `RLock` correctly allows the *same* thread to re-enter while still blocking a
*different* thread (a separate session's `run_task()` call) exactly as intended. Caught before
shipping by re-reading the code that calls into this, not by a test catching a hang — there's no
regression test for a deadlock that would only reproduce inside a real autonomous run.
"""
from __future__ import annotations

import threading

_registry_lock = threading.Lock()
_engagement_locks: dict[str, threading.RLock] = {}


def engagement_lock(engagement_id: str) -> threading.RLock:
    """Returns the one RLock for this engagement_id, creating it on first use. Safe to call
    concurrently — registry access itself is guarded, and creating-then-returning a lock object
    is not a race (the lock isn't acquired here, just looked up/created)."""
    with _registry_lock:
        lock = _engagement_locks.get(engagement_id)
        if lock is None:
            lock = threading.RLock()
            _engagement_locks[engagement_id] = lock
        return lock
