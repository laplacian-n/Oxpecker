"""Consult queue — the autonomous pipeline driver's "consult" mode check-in mechanism
(agent/pipeline/autonomous_driver.py). Same file-based/atomic-write/poll pattern as
agent/broker/approval_queue.py (this project's own established primitive shape — see that
module's `_write_atomic` for why atomic writes matter here too, the same concurrent
submit-while-polling race that bit ApprovalQueue applies here identically), but shaped for a
free-text question with an optional set of suggested options and a free-text answer, not a
boolean approve/decline — a driver phase-boundary check-in ("here's what happened, want to
continue, redirect, or stop?") isn't a yes/no gate, so ApprovalQueue's shape doesn't fit it.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .. import config

QUEUE_DIR = config.STATE_DIR / "broker" / "consult_queue"
VALID_STATUSES = ("pending", "answered")


class ConsultRequestNotFoundError(RuntimeError):
    pass


class AlreadyAnsweredError(RuntimeError):
    pass


def _write_atomic(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)


class ConsultQueue:
    def __init__(self, queue_dir: Path | None = None):
        self.queue_dir = queue_dir if queue_dir is not None else QUEUE_DIR
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    def submit(
        self, *, session_id: str, question: str, context: dict | None = None,
        options: list[str] | None = None,
    ) -> str:
        request_id = str(uuid.uuid4())
        record = {
            "request_id": request_id,
            "session_id": session_id,
            "question": question,
            "context": context or {},
            "options": options or [],
            "status": "pending",
            "created_at": time.time(),
            "answered_at": None,
            "answered_by": None,
            "answer": None,
        }
        _write_atomic(self.queue_dir / f"{request_id}.json", json.dumps(record, indent=2))
        return request_id

    def get(self, request_id: str) -> dict:
        path = self.queue_dir / f"{request_id}.json"
        if not path.exists():
            raise ConsultRequestNotFoundError(request_id)
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

    def resolve(self, request_id: str, *, answer: str, resolved_by: str) -> dict:
        path = self.queue_dir / f"{request_id}.json"
        if not path.exists():
            raise ConsultRequestNotFoundError(request_id)
        record = json.loads(path.read_text())
        if record["status"] != "pending":
            raise AlreadyAnsweredError(f"request {request_id} already answered")
        record["status"] = "answered"
        record["answer"] = answer
        record["answered_at"] = time.time()
        record["answered_by"] = resolved_by
        _write_atomic(path, json.dumps(record, indent=2))
        return record

    def wait_for_answer(
        self, request_id: str, timeout_s: float, poll_interval_s: float = 0.2
    ) -> dict:
        deadline = time.time() + timeout_s
        while True:
            record = self.get(request_id)
            if record["status"] != "pending":
                return record
            if time.time() >= deadline:
                raise TimeoutError(f"consult request {request_id} not answered within {timeout_s}s")
            time.sleep(poll_interval_s)
