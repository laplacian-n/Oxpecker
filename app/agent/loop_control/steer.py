"""Phase 6 — steer channel. Lets an operator inject a mid-task message without stopping the
agent loop, file-based (same session-scoped pattern as agent/broker/taint.py and the kill
switch), so it works across process boundaries. This is a standalone, tested primitive — it is
NOT yet wired into agent/loop.py's actual per-iteration read (see docs/STATUS.md's Phase 6 gaps
row): loop.py is a shared file another live process (the eval harness) reads/writes during this
pass, and this project's own established lesson from earlier in this build is not to touch files
under active use by a running background process — wiring this in is deferred to a separate,
isolated pass rather than risking that.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config

STEER_DIR = config.STATE_DIR / "loop_control" / "steer"


class SteerChannel:
    def __init__(self, session_id: str, steer_dir: Path | None = None):
        self.session_id = session_id
        base = steer_dir if steer_dir is not None else STEER_DIR
        self.path = base / f"{session_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, message: str, *, sender: str = "operator") -> None:
        record = {"message": message, "sender": sender, "sent_at": time.time(), "consumed": False}
        self.path.write_text(json.dumps(record))

    def peek_pending(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            record = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return None
        return None if record.get("consumed") else record

    def take_pending(self) -> str | None:
        """Returns the pending message text and marks it consumed, or None if none pending."""
        record = self.peek_pending()
        if record is None:
            return None
        record["consumed"] = True
        self.path.write_text(json.dumps(record))
        return record["message"]
