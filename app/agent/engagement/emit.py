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
from pathlib import Path

from .. import config
from .event_log import EngagementEventLog

log = logging.getLogger("agent.engagement.emit")


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
        return EngagementEventLog(config.ENGAGEMENTS_ROOT / engagement_id).append(kind, payload or {})
    except Exception:  # noqa: BLE001 - telemetry must never break its producer
        log.warning("failed to emit %r event for engagement %r", kind, engagement_id, exc_info=True)
        return None


def engagement_id_from_dir(engagement_dir: Path | str) -> str:
    """The engagement id is the engagement directory's name — the convention EngagementStore and
    the web runtime already use (`ENGAGEMENTS_ROOT/<id>/...`)."""
    return Path(engagement_dir).name
