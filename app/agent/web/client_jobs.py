"""Client-side tool execution ("this computer" isolation).

In *cloud* isolation the server runs a tool's actual I/O itself, inside its sandbox. In
*this computer* isolation the server still does everything else — drives the agent loop,
calls the model, and (critically) enforces scope BEFORE anything runs — but the final
network/command I/O is handed to the operator's own desktop app to run from THAT machine's
network position. That is the whole point: probe a target from the laptop that is actually on
its network, while the brains and the scope gate stay on the server.

This module is the server half of that handoff: a small thread-safe broker.

  - A tool-dispatch thread calls `submit(...)`, which parks the job and blocks until the
    desktop app returns a result (or it times out / no app is connected).
  - The desktop app long-polls `claim(...)` for the next job, runs it, and posts the result
    back via `deliver(...)`.

Everything here runs on worker threads (tool dispatch is synchronous and already threaded), so
blocking waits are fine and there is no asyncio in this file.

Security note: this broker does NOT decide what may run. Scope (RoE allowlist), the broker's
action-class gate and the deny list are all applied in dev_server BEFORE a job is ever
submitted. By the time a payload reaches here it has already been authorised for its target;
the desktop app is only an executor of an already-permitted action.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# A job whose desktop executor never shows up should fail fast rather than hang the whole agent
# turn for the full tool timeout. If nothing claims a submitted job within this many seconds, we
# treat "this computer" as not connected and raise.
CONNECT_GRACE_S = 20.0


class ExecutorNotConnected(Exception):
    """No desktop app claimed the job in time — nothing is executing on this computer."""


class ClientJobTimeout(Exception):
    """The desktop app claimed the job but did not return a result in time."""


@dataclass
class _Job:
    id: str
    session_id: str
    kind: str
    payload: dict
    created_at: float = field(default_factory=time.time)
    claimed: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None


class ClientJobBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: list[_Job] = []       # submitted, not yet claimed (FIFO)
        self._by_id: dict[str, _Job] = {}     # all live jobs (pending + claimed, not done)

    # ── submitter side (tool-dispatch thread) ──────────────────────────────
    def submit(self, session_id: str, kind: str, payload: dict, *, timeout_s: float) -> dict:
        """Park a job and block until the desktop app returns its result.

        Raises ExecutorNotConnected if no app claims it within CONNECT_GRACE_S, or
        ClientJobTimeout if it is claimed but no result comes back within timeout_s.
        """
        job = _Job(id=uuid.uuid4().hex[:16], session_id=session_id, kind=kind, payload=payload)
        with self._cond:
            self._pending.append(job)
            self._by_id[job.id] = job
            self._cond.notify_all()
        try:
            # Phase 1: wait for a claim, but not longer than the connect grace.
            if not self._wait_claimed(job, CONNECT_GRACE_S):
                raise ExecutorNotConnected(
                    "no 'this computer' executor is connected — open the Oxpecker desktop app "
                    "with this server selected so it can run the command on your machine."
                )
            # Phase 2: it is being executed; wait for the result up to the tool timeout.
            if not job.done.wait(timeout=timeout_s):
                raise ClientJobTimeout(
                    f"the command was sent to this computer but did not return within {timeout_s:.0f}s."
                )
            return job.result if isinstance(job.result, dict) else {"ok": False, "error": "empty result"}
        finally:
            with self._cond:
                self._by_id.pop(job.id, None)
                if job in self._pending:
                    self._pending.remove(job)

    def _wait_claimed(self, job: _Job, grace_s: float) -> bool:
        deadline = time.time() + grace_s
        with self._cond:
            while not job.claimed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return job.claimed
                self._cond.wait(timeout=remaining)
            return True

    # ── executor side (desktop app long-poll) ──────────────────────────────
    def claim(self, *, wait_s: float) -> dict | None:
        """Return the next unclaimed job as a wire dict, or None after wait_s with nothing to do.

        Marks the job claimed so submit() stops waiting for a connection and starts its result
        clock. A long-poll so the desktop app can hold one request open instead of busy-polling.
        """
        deadline = time.time() + wait_s
        with self._cond:
            while True:
                while self._pending:
                    job = self._pending.pop(0)
                    if job.id not in self._by_id:
                        continue  # submitter already gave up; skip it
                    job.claimed = True
                    self._cond.notify_all()
                    return {"id": job.id, "session_id": job.session_id,
                            "kind": job.kind, "payload": job.payload}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    def deliver(self, job_id: str, result: dict) -> bool:
        """Hand a result back to the waiting submitter. Returns False if nobody is waiting."""
        with self._cond:
            job = self._by_id.get(job_id)
            if job is None:
                return False
            job.result = result
            job.done.set()
            self._cond.notify_all()
            return True
