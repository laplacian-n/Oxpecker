"""Phase 6 — session handoff. A thin combinator, not a new state store: it snapshots the
already-authoritative state from two existing systems (the memory service's session, Phase 2 —
`agent/memory_service/db.py`; and this engagement's phase/task/hypothesis state, M5.2 —
`agent/engagement/store.py`) plus an operator's free-text note, into one file another operator or
device can read to pick up an engagement without piecing the two together by hand. It does not
duplicate or take over either system's actual state.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .store import EngagementStore

HANDOFFS_SUBDIR = "handoffs"


def create_handoff(
    engagement_dir: Path, memory_session: dict, note: str, handed_off_by: str
) -> Path:
    """`memory_session` is whatever `agent.memory_service.db.Store.get_session()` returned for
    the session being handed off — passed in rather than fetched here, since the memory service
    may be a separate remote process this module has no business reaching into directly."""
    store = EngagementStore(engagement_dir)
    phase = store.get_phase()
    open_hypotheses = store.list_hypotheses(status="open") + store.list_hypotheses(status="testing")
    pending_tasks = [
        t for t in store.list_tasks() if t["status"] in ("pending", "running", "blocked")
    ]
    coverage = store.coverage_summary()

    record = {
        "handoff_id": str(uuid.uuid4()),
        "created_at": time.time(),
        "handed_off_by": handed_off_by,
        "note": note,
        "memory_session": memory_session,
        "phase": phase["current_phase"],
        "phase_version": phase["version"],
        "open_or_testing_hypothesis_count": len(open_hypotheses),
        "pending_or_running_task_count": len(pending_tasks),
        "coverage_summary": coverage,
    }

    handoffs_dir = engagement_dir / HANDOFFS_SUBDIR
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / f"{record['handoff_id']}.json"
    path.write_text(json.dumps(record, indent=2))
    return path


def latest_handoff(engagement_dir: Path) -> dict | None:
    handoffs_dir = engagement_dir / HANDOFFS_SUBDIR
    if not handoffs_dir.exists():
        return None
    paths = sorted(handoffs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not paths:
        return None
    return json.loads(paths[-1].read_text())


def list_handoffs(engagement_dir: Path) -> list[dict]:
    handoffs_dir = engagement_dir / HANDOFFS_SUBDIR
    if not handoffs_dir.exists():
        return []
    paths = sorted(handoffs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return [json.loads(p.read_text()) for p in paths]
