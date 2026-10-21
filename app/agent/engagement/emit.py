"""Append a §4.2 event to an engagement's event log, from wherever a producer runs.

CLIENT_UI_DESIGN.md §4 built the pipe (the engagement event log) and step 0 left the producers
for later: "a producer that emits a kind this fold has not learned yet still appears in the
stream". This is the seam those producers call. The broker, the hypothesis graph, the notebook
and the findings store all know their engagement; they call `emit(engagement_id, kind, payload)`
and the event lands on the one ordered, persistent, engagement-wide stream every surface and the
trajectory spine (§8) draw from.

Two rules this enforces so instrumentation never changes behaviour:

  * **Telemetry never breaks its producer.** Every append is wrapped — an event log that cannot
    be written (disk full, a torn file) must not take down the tool call or the graph write it
    was describing. It returns None and logs, rather than raising into the caller.
  * **One atomic append.** It reuses `EngagementEventLog.append`, whose sequence number is
    allocated inside the write (§4.1.1), so several producers in several threads/processes
    emitting at once cannot collide — the property the whole log exists to guarantee.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .. import config
from .event_log import EngagementEventLog

log = logging.getLogger("agent.engagement.emit")

# Which worker a thread is currently running as, so a producer deep in the stack (the AgentLoop's
# tool execution) can tag its events with the worker_id the wave assigned — without threading that
# id through every call. A wave sets it on the worker's own thread before the loop runs (wave.py);
# the assistant/chat path never sets it, so current_worker() is None there and tool events fall
# back to the session id. Thread-local because each wave worker runs on its own thread.
_worker_ctx = threading.local()


def set_current_worker(worker_id: str | None) -> None:
    _worker_ctx.worker_id = worker_id


def current_worker() -> str | None:
    return getattr(_worker_ctx, "worker_id", None)


# In-process listeners run after each successful append, so a surface in THIS process (the web
# runtime, during an autonomous run) can mirror live wave/worker events into a session chat without
# polling the log. Out-of-process consumers still read the persisted log; this is additive and only
# ever a best-effort notification — like the append itself, a listener that raises is swallowed so
# it can never break the producer it is observing.
_listeners_lock = threading.Lock()
_listeners: list = []


def subscribe(callback) -> object:
    """Register callback(engagement_id, kind, payload, seq) to run after each successful emit.
    Returns the callback so the caller can unsubscribe it. In-process only."""
    with _listeners_lock:
        _listeners.append(callback)
    return callback


def unsubscribe(callback) -> None:
    with _listeners_lock:
        try:
            _listeners.remove(callback)
        except ValueError:
            pass


def emit(engagement_id: str, kind: str, payload: dict | None = None) -> int | None:
    """Append one event; return its sequence number, or None if the append could not be made.
    Never raises into the caller — a producer's real work must not fail because its telemetry did.

    The log's directory is resolved from `config.ENGAGEMENTS_ROOT` at call time, deliberately NOT
    cached across calls: a cached EngagementEventLog would bind the root computed on its first use,
    so a test that patches `config.ENGAGEMENTS_ROOT` afterwards would have its events written to
    the real engagements dir instead — the bound-default hazard this codebase keeps relearning.
    Constructing one per call is cheap (append opens its own connection regardless).
    """
    if not engagement_id:
        return None
    try:
        seq = EngagementEventLog(config.ENGAGEMENTS_ROOT / engagement_id).append(kind, payload or {})
    except Exception:  # noqa: BLE001 - telemetry must never break its producer
        log.warning("failed to emit %r event for engagement %r", kind, engagement_id, exc_info=True)
        return None
    if seq is not None and _listeners:
        with _listeners_lock:
            listeners = list(_listeners)
        for cb in listeners:
            try:
                cb(engagement_id, kind, payload or {}, seq)
            except Exception:  # noqa: BLE001 - a listener must never break its producer either
                log.warning("emit listener failed for %r event on %r", kind, engagement_id,
                            exc_info=True)
    return seq


def engagement_id_from_dir(engagement_dir: Path | str) -> str:
    """The engagement id is the engagement directory's name — the convention EngagementStore and
    the web runtime already use (`ENGAGEMENTS_ROOT/<id>/...`)."""
    return Path(engagement_dir).name
