"""Phase 6 — approval queue. A backend primitive: persists pending human-approval requests so
they can be approved/declined out-of-band, decoupled from the synchronous `input()` prompt
`Broker.dispatch` currently blocks on (agent/broker/broker.py, unchanged by this module). This
is deliberately additive — the existing CLI approval path remains the default and this queue is
not yet wired into `dispatch()`; see docs/STATUS.md's Phase 6 gaps row. The actual UI a real
operator would use to see/act on this queue is explicitly out of scope for this pass (per "finish
everything until the UI").
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .. import config

QUEUE_DIR = config.STATE_DIR / "broker" / "approval_queue"
VALID_STATUSES = ("pending", "approved", "declined")


class ApprovalRequestNotFoundError(RuntimeError):
    pass


class AlreadyResolvedError(RuntimeError):
    pass


def _write_atomic(path: Path, content: str) -> None:
    """`Path.write_text()` is not atomic — a concurrent reader (list_pending() polling from
    another thread, exactly the pattern the Phase 6 web UI uses) can observe a truncated/empty
    file mid-write. Found for real via a genuine race in agent/web/test_server.py, not
    hypothetically: a background thread's submit() and the test's polling list_pending() call
    interleaved closely enough to hit it. Fixed with the standard write-to-temp-then-rename
    pattern — `os.replace()` is atomic on the same filesystem, so a reader only ever sees the
    old complete file or the new complete file, never a partial one."""
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)


class ApprovalQueue:
    def __init__(self, queue_dir: Path | None = None):
        self.queue_dir = queue_dir if queue_dir is not None else QUEUE_DIR
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    def submit(self, *, session_id: str, tool: str, arguments: dict, reason: str) -> str:
        request_id = str(uuid.uuid4())
        record = {
            "request_id": request_id,
            "session_id": session_id,
            "tool": tool,
            "arguments": arguments,
            "reason": reason,
            "status": "pending",
            "created_at": time.time(),
            "resolved_at": None,
            "resolved_by": None,
        }
        _write_atomic(self.queue_dir / f"{request_id}.json", json.dumps(record, indent=2))
        return request_id

    def get(self, request_id: str) -> dict:
        path = self.queue_dir / f"{request_id}.json"
        if not path.exists():
            raise ApprovalRequestNotFoundError(request_id)
        return json.loads(path.read_text())

    def list_pending(self, session_id: str | None = None) -> list[dict]:
        out = []
        for path in sorted(self.queue_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue  # mid-write from another process/thread; it'll be complete next poll
            if record["status"] != "pending":
                continue
            if session_id is not None and record["session_id"] != session_id:
                continue
            out.append(record)
        return out

    def resolve(self, request_id: str, *, approved: bool, resolved_by: str) -> dict:
        path = self.queue_dir / f"{request_id}.json"
        if not path.exists():
            raise ApprovalRequestNotFoundError(request_id)
        record = json.loads(path.read_text())
        if record["status"] != "pending":
            raise AlreadyResolvedError(f"request {request_id} already {record['status']}")
        record["status"] = "approved" if approved else "declined"
        record["resolved_at"] = time.time()
        record["resolved_by"] = resolved_by
        _write_atomic(path, json.dumps(record, indent=2))
        return record

    def wait_for_resolution(
        self, request_id: str, timeout_s: float, poll_interval_s: float = 0.2
    ) -> dict:
        deadline = time.time() + timeout_s
        while True:
            record = self.get(request_id)
            if record["status"] != "pending":
                return record
            if time.time() >= deadline:
                raise TimeoutError(f"approval request {request_id} not resolved within {timeout_s}s")
            time.sleep(poll_interval_s)
